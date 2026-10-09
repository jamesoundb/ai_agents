#!/usr/bin/env python3
"""kubegraph.py -- a graph of a live Kubernetes cluster, built from read-only API reads.

`snapshot` lists every listable resource type the cluster serves (built-in kinds and custom resources
alike) with `kubectl get -o json`, derives each object's health from its status, links the objects
(ownership, selectors, ConfigMap/Secret/PVC/ServiceAccount references, routing, scaling, webhooks,
APIServices, CRD instances) and stores the result in SQLite. Every other command answers one question
from that snapshot, refreshing it first when it is older than --max-age.

  kubegraph.py snapshot [--context C] [-n NS ...] [--save DIR]   collect and build
  kubegraph.py snapshot --from DIR                                 build from saved `kubectl get -o json` lists
  kubegraph.py health [-n NS]           unhealthy objects grouped by their top owner, cluster-scoped first
  kubegraph.py why OBJ...               cause chain: owners, children, dependencies, events, logs, next step
  kubegraph.py show OBJ...              card: status, resources, owner, children, uses, used by
  kubegraph.py used-by OBJ              everything that depends on OBJ (impact of changing or deleting it)
  kubegraph.py tree OBJ | -n NS         ownership tree with health
  kubegraph.py find PATTERN             objects by name (glob or substring), --kind, -n, --unhealthy
  kubegraph.py events [OBJ] [-n NS]     recent events, warnings first
  kubegraph.py crds                     custom resource types, instance counts and health
  kubegraph.py overview                 namespaces: pods, requests vs usage, unhealthy counts

Read-only by construction: kubectl() refuses any verb other than get, api-resources, logs, version and
`config current-context`. Secret values are never stored (data/stringData are reduced to key names),
ConfigMaps keep key names only, the last-applied annotation and managedFields are dropped, and string
values under credential-like keys are redacted. Stdlib only, Python 3.10+.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import difflib
import fnmatch
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

VERSION = "1"
KUBECTL = os.environ.get("KUBEGRAPH_KUBECTL", "kubectl")
DB_DIR = os.environ.get("KUBEGRAPH_DIR", os.path.join(os.path.expanduser("~"), ".cache", "kubegraph"))
MAX_AGE = int(os.environ.get("KUBEGRAPH_MAX_AGE", "300"))   # seconds before a query re-snapshots

# Resource types not worth a list call: duplicates of another type (events.k8s.io, endpoints and
# endpoint slices are derived from Services + Pods here), high-churn bookkeeping (leases, controller
# revisions) or deprecated. `--include` brings one back.
SKIP = {"events.events.k8s.io", "leases.coordination.k8s.io", "controllerrevisions.apps", "endpoints",
        "endpointslices.discovery.k8s.io", "componentstatuses", "podtemplates",
        "csistoragecapacities.storage.k8s.io"}
METRICS = {"pods.metrics.k8s.io", "nodes.metrics.k8s.io"}   # usage, attached to Pods and Nodes
LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"
CRED_KEY = re.compile(r"^(password|passwd|pwd|token|apikey|api[-_]key|secret|clientsecret|client[-_]secret|"
                      r"privatekey|private[-_]key|accesskey|access[-_]key|secretkey|secret[-_]key)$", re.I)
CRED_ENV = re.compile(r"pass|secret|token|key|cred|auth", re.I)
SYSTEM_NS = ("kube-system", "kube-public", "kube-node-lease", "gke-managed-system", "gmp-system")

# Output caps; each has a flag that lifts it.
CAP_GROUPS = 40      # health: owner groups (--max-rows)
CAP_LIST = 12        # card sections (--all)
CAP_EVENTS = 8       # events per why/show (--all)
CAP_FIND = 40        # find rows (--max-rows)
LOG_TAIL = 20        # why: log lines per failing container (--tail)

# What to do about each failure reason. The class decides the advice; the snapshot gives the evidence.
ADVICE = {
    "CrashLoopBackOff": "the container exits right after start: read the previous instance's logs; check "
                        "command/args, config and the services it needs at startup",
    "Error": "the container exited non-zero: read its logs",
    "Killed": "SIGKILL (exit 137) without an OOMKilled record: usually the memory limit (compare it with "
              "peak usage), otherwise a failed liveness probe (check Unhealthy events)",
    "OOMKilled": "memory limit too low for the workload (or a leak): compare the limit with peak usage, "
                 "raise the limit or fix the leak",
    "ImagePullBackOff": "image name/tag wrong, registry unreachable, or missing imagePullSecrets",
    "ErrImagePull": "image name/tag wrong, registry unreachable, or missing imagePullSecrets",
    "InvalidImageName": "the image reference does not parse",
    "CreateContainerConfigError": "a referenced ConfigMap/Secret (or one of its keys) does not exist",
    "CreateContainerError": "the runtime could not create the container: see the message (mounts, "
                            "security context, command)",
    "Unschedulable": "no node fits: the message lists each node's reason (requests vs free capacity, "
                     "nodeSelector/affinity, taints, PVC zone)",
    "ContainerCreating": "stuck before start: usually a volume (FailedMount events) or the CNI",
    "ProbeFailing": "the readiness/liveness probe fails: check the probe path/port and app startup time",
    "NotReady": "running but not Ready: check readiness probe events and app logs",
    "Evicted": "the kubelet evicted the pod under node pressure (memory/disk): see the message",
    "NoPods": "the Service selector matches no pod: compare its labels with the workload's pod labels",
    "NoReadyEndpoints": "the Service's pods exist but none is Ready: fix the pods",
    "missing": "a referenced object does not exist: create it or fix the reference",
    "Pending": "the claim is not bound: check the StorageClass and the provisioner's events",
    "ReplicaFailure": "the controller cannot create pods: quota, LimitRange, admission webhook or "
                      "PodSecurity (message says which)",
    "ProgressDeadlineExceeded": "the rollout stalled: the new pods never became available",
    "BackoffLimitExceeded": "the Job's pods kept failing: read the last pod's logs",
    "DeadlineExceeded": "the Job ran past activeDeadlineSeconds",
    "FailedGetResourceMetric": "the HPA has no metric: target pods lack resource requests or "
                               "metrics-server is down",
    "NodeNotReady": "the kubelet stopped reporting: node down, network, or kubelet failure",
    "WebhookUnavailable": "an admission webhook has no backend: with failurePolicy Fail it blocks "
                          "creating or updating matched objects",
    "ServiceNotFound": "the aggregated API's backend Service is gone: its API group is unavailable",
    "FailedDiscoveryCheck": "the aggregated API's backend does not answer: check its pods",
    "QuotaExhausted": "the namespace ResourceQuota is used up: new pods are rejected",
    "RolloutStuck": "the new revision's pods fail while the old revision keeps serving: fix the change that "
                    "went into the new pod template (image, config, probes), or roll the manifest back",
}


class QueryError(Exception):
    pass


# --------------------------------------------------------------------------- kubectl (read-only)

def kubectl(ctx, args, timeout=60):
    """Run one read-only kubectl call; returns (rc, stdout, stderr)."""
    verb = args[0]
    if verb not in ("get", "api-resources", "logs", "version") and args[:2] != ["config", "current-context"]:
        raise SystemExit(f"kubegraph: refusing a kubectl call that is not read-only: {' '.join(args)}")
    cmd = [KUBECTL] + (["--context", ctx] if ctx else []) + list(args)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise SystemExit(f"kubegraph: {KUBECTL} not found (set KUBEGRAPH_KUBECTL)") from None
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    return p.returncode, p.stdout, p.stderr


def clean_err(err):
    """kubectl stderr without client-side discovery noise (memcache lines about unavailable API groups)."""
    return [l for l in err.strip().splitlines()
            if l and "memcache.go" not in l and "couldn't get resource list" not in l]


def current_context():
    rc, out, err = kubectl(None, ["config", "current-context"], timeout=15)
    if rc != 0 or not out.strip():
        raise SystemExit(f"kubegraph: no current kubectl context ({err.strip()}); pass --context")
    return out.strip()


def parse_api_resources(text):
    """`kubectl api-resources -o wide` table -> [{name, short, group, namespaced, kind, verbs}].
    Columns are located from the header, so an empty SHORTNAMES cell does not shift the row."""
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return []
    hdr = lines[0]
    cols = [c for c in ("NAME", "SHORTNAMES", "APIVERSION", "APIGROUP", "NAMESPACED", "KIND", "VERBS", "CATEGORIES")
            if re.search(rf"(^|\s){c}(\s|$)", hdr)]
    starts = sorted((re.search(rf"(^|\s)({c})(\s|$)", hdr).start(2), c) for c in cols)
    out = []
    for line in lines[1:]:
        row = {}
        for i, (pos, c) in enumerate(starts):
            end = starts[i + 1][0] if i + 1 < len(starts) else None
            row[c] = line[pos:end].strip()
        if "APIVERSION" in row:
            av = row["APIVERSION"]
            group = av.split("/")[0] if "/" in av else ""
        else:
            group = row.get("APIGROUP", "")
        verbs = row.get("VERBS", "").strip("[]").split()
        out.append({"name": row.get("NAME", ""), "short": [s for s in row.get("SHORTNAMES", "").split(",") if s],
                    "group": group, "namespaced": row.get("NAMESPACED", "").lower() == "true",
                    "kind": row.get("KIND", ""), "verbs": verbs})
    return [r for r in out if r["name"] and r["kind"]]


def qname(res):
    return res["name"] if not res["group"] else f"{res['name']}.{res['group']}"


# --------------------------------------------------------------------------- sanitizing

def _redact(node, parent_key=""):
    if isinstance(node, dict):
        # env entries: keep the name and the valueFrom reference; drop credential-looking values
        if "name" in node and "value" in node and parent_key == "env" and CRED_ENV.search(str(node["name"])):
            node["value"] = "<redacted>"
        for k, v in list(node.items()):
            if isinstance(v, str) and CRED_KEY.match(k):
                node[k] = "<redacted>"
            elif k == "caBundle":
                node[k] = "<dropped>"
            else:
                _redact(v, k)
    elif isinstance(node, list):
        for v in node:
            _redact(v, parent_key)


def sanitize(obj):
    """Strip what must not be stored (secret values) and what only costs space."""
    md = obj.get("metadata") or {}
    md.pop("managedFields", None)
    ann = md.get("annotations") or {}
    if LAST_APPLIED in ann:
        ann[LAST_APPLIED] = "<dropped>"
    kind = obj.get("kind")
    if (kind == "Secret" or kind == "ConfigMap") and "dataKeys" not in obj:   # idempotent: --save re-sanitizes
        keys = sorted(set((obj.get("data") or {}).keys()) | set((obj.get("stringData") or {}).keys())
                      | set((obj.get("binaryData") or {}).keys()))
        for k in ("data", "stringData", "binaryData"):
            obj.pop(k, None)
        obj["dataKeys"] = keys
    if kind == "CustomResourceDefinition":
        for v in (obj.get("spec") or {}).get("versions") or []:
            v.pop("schema", None)
        (obj.get("spec") or {}).pop("validation", None)
    _redact(obj)
    return obj


# --------------------------------------------------------------------------- quantities and time

def cpu(q):
    if q in (None, ""):
        return 0.0
    s = str(q)
    for suf, mul in (("n", 1e-9), ("u", 1e-6), ("m", 1e-3)):
        if s.endswith(suf):
            return float(s[:-1]) * mul
    return float(s)


_MEM = {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4, "Pi": 1024 ** 5, "Ei": 1024 ** 6,
        "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18, "m": 1e-3}


def mem(q):
    if q in (None, ""):
        return 0.0
    s = str(q)
    for suf in sorted(_MEM, key=len, reverse=True):
        if s.endswith(suf):
            return float(s[: -len(suf)]) * _MEM[suf]
    return float(s)


def fcpu(c):
    return f"{c:.2f}" if c < 10 else f"{c:.0f}"


def fmem(b):
    if b >= 1024 ** 3:
        return f"{b / 1024 ** 3:.1f}Gi"
    return f"{b / 1024 ** 2:.0f}Mi"


def ts(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def ago(seconds):
    if seconds is None:
        return "?"
    s = int(max(seconds, 0))
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit}"
    return f"{s}s"


def pod_requests(spec):
    """Effective pod requests: max(sum of containers, largest init container); requests default to limits."""
    def one(c):
        r = (c.get("resources") or {})
        req, lim = r.get("requests") or {}, r.get("limits") or {}
        return cpu(req.get("cpu", lim.get("cpu"))), mem(req.get("memory", lim.get("memory")))
    main = [one(c) for c in spec.get("containers") or []]
    init = [one(c) for c in spec.get("initContainers") or []]
    c = sum(x[0] for x in main); m = sum(x[1] for x in main)
    if init:
        c = max(c, max(x[0] for x in init)); m = max(m, max(x[1] for x in init))
    return c, m


# --------------------------------------------------------------------------- health per kind

def cond_map(st):
    return {c.get("type"): c for c in (st or {}).get("conditions") or [] if isinstance(c, dict)}


def pod_health(o, now):
    st, spec, md = o.get("status") or {}, o.get("spec") or {}, o.get("metadata") or {}
    phase = st.get("phase", "Unknown")
    statuses = st.get("containerStatuses") or []
    ready = sum(1 for c in statuses if c.get("ready"))
    total = len(spec.get("containers") or [])
    restarts = sum(c.get("restartCount", 0) for c in statuses)
    node = spec.get("nodeName") or "-"
    summary = f"{phase} {ready}/{total} ready, {restarts} restarts, node {node}"
    age = now - (ts(md.get("creationTimestamp")) or now)
    if md.get("deletionTimestamp"):
        late = now - (ts(md["deletionTimestamp"]) or now)
        if late > 120:
            return "warn", "StuckTerminating", f"terminating for {ago(late)} (finalizers {md.get('finalizers') or []})"
    if st.get("reason") == "Evicted":
        return "fail", "Evicted", (st.get("message") or "")[:200]
    if phase == "Succeeded":
        return "ok", "Completed", summary
    # container-level causes, init containers first (they block the rest)
    for init, group in ((True, st.get("initContainerStatuses") or []), (False, statuses)):
        for cs in group:
            state, last = cs.get("state") or {}, (cs.get("lastState") or {}).get("terminated") or {}
            w, t = state.get("waiting"), state.get("terminated")
            who = f"{'init container' if init else 'container'} {cs.get('name')}"
            if w and w.get("reason") not in (None, "ContainerCreating", "PodInitializing"):
                r = w["reason"]
                if last.get("reason") == "OOMKilled":
                    return "fail", "OOMKilled", f"{who} OOMKilled (exit {last.get('exitCode')}), now {r}, {cs.get('restartCount', 0)} restarts{limit_note(spec, cs.get('name'))}"
                if r == "CrashLoopBackOff" and last.get("exitCode") == 137:
                    r = "Killed"   # SIGKILL without an OOMKilled record: memory limit or liveness kill
                if r in ("CrashLoopBackOff", "Killed") and last:
                    return "fail", r, f"{who} exit {last.get('exitCode')} ({last.get('reason')}), {cs.get('restartCount', 0)} restarts{exit_note(last.get('exitCode'), spec, cs.get('name'))}"
                return "fail", r, f"{who}: {(w.get('message') or r)[:200]}"
            # a crash cycle: the previous instance died within minutes of starting. One that ran for hours and
            # stopped with its node (a reboot) is not why the pod is unready now.
            lived = (ts(last.get("finishedAt")) or 0) - (ts(last.get("startedAt")) or 0)
            if not cs.get("ready") and cs.get("restartCount", 0) > 0 and last and lived < 600 and "terminated" not in state:
                # between restarts the container shows Running: the last termination is the cause
                r = last.get("reason") or "Error"
                return "fail", r, f"{who} exit {last.get('exitCode')} ({r}), {cs.get('restartCount', 0)} restarts{limit_note(spec, cs.get('name')) if r == 'OOMKilled' else exit_note(last.get('exitCode'), spec, cs.get('name'))}"
            if t and t.get("exitCode", 0) != 0:   # just exited (the kubelet restarts it, or the pod failed)
                r = t.get("reason") or "Error"
                return "fail", r, f"{who} exit {t.get('exitCode')} ({r}){limit_note(spec, cs.get('name')) if r == 'OOMKilled' else exit_note(t.get('exitCode'), spec, cs.get('name'))}"
    if phase == "Failed":
        return "fail", st.get("reason") or "Failed", (st.get("message") or summary)[:200]
    if phase == "Unknown":
        return "fail", "Unknown", "pod status unknown: its node stopped reporting"
    conds = cond_map(st)
    sched = conds.get("PodScheduled")
    if phase == "Pending" and sched and sched.get("status") == "False":
        return "fail", sched.get("reason") or "Unschedulable", (sched.get("message") or "")[:300]
    if phase == "Pending":
        if age > 120:
            return "fail", "ContainerCreating", f"pending for {ago(age)}"
        return "warn", "Starting", f"pending for {ago(age)}"
    if total and ready < total:
        if age > 120:
            return "fail", "NotReady", summary
        return "warn", "Starting", summary
    for cs in statuses:
        last = (cs.get("lastState") or {}).get("terminated") or {}
        lived = (ts(last.get("finishedAt")) or 0) - (ts(last.get("startedAt")) or 0)
        # restarts count only when the last instance died young (or of OOM): node reboots restart everything
        if (cs.get("restartCount", 0) >= 3 and last and lived < 600) or last.get("reason") == "OOMKilled":
            return "warn", "Restarting", f"container {cs.get('name')} {cs.get('restartCount', 0)} restarts, last exit {last.get('exitCode')} ({last.get('reason')})"
    return "ok", "Running", summary


def exit_note(code, spec, cname):
    if code == 137:
        return f" (137 = SIGKILL: out of memory{limit_note(spec, cname)}, or a failed liveness probe)"
    if code == 143:
        return " (143 = SIGTERM)"
    return ""


def limit_note(spec, cname):
    for c in (spec.get("containers") or []) + (spec.get("initContainers") or []):
        if c.get("name") == cname:
            lim = ((c.get("resources") or {}).get("limits") or {}).get("memory")
            return f", memory limit {lim}" if lim else ", no memory limit"
    return ""


def replicas_health(kind, o):
    spec, st = o.get("spec") or {}, o.get("status") or {}
    conds = cond_map(st)
    want = spec.get("replicas", 1)
    ready = st.get("readyReplicas", 0) or 0
    avail = st.get("availableReplicas", ready) or 0
    summary = f"{ready}/{want} ready"
    rf = conds.get("ReplicaFailure")
    if rf and rf.get("status") == "True":
        return "fail", "ReplicaFailure", f"{summary}; {(rf.get('message') or '')[:250]}"
    pr = conds.get("Progressing")
    if pr and pr.get("status") == "False":
        return "fail", pr.get("reason") or "ProgressDeadlineExceeded", f"{summary}; {(pr.get('message') or '')[:200]}"
    if want == 0:
        return "ok", "ScaledToZero", "0 replicas"
    if kind == "Deployment" and avail == 0 or kind != "Deployment" and ready == 0:
        return "fail", "Unavailable", f"0/{want} available"
    if ready < want:
        return "warn", "Degraded", summary
    if kind == "Deployment" and (st.get("updatedReplicas", want) or 0) < want:
        return "warn", "RollingOut", f"{summary}, {st.get('updatedReplicas', 0)}/{want} updated"
    return "ok", "Ready", summary


def daemonset_health(o):
    st = o.get("status") or {}
    want, ready = st.get("desiredNumberScheduled", 0), st.get("numberReady", 0)
    summary = f"{ready}/{want} ready"
    if st.get("numberMisscheduled"):
        return "warn", "Misscheduled", f"{summary}, {st['numberMisscheduled']} misscheduled"
    if want and ready == 0:
        return "fail", "Unavailable", summary
    if ready < want:
        return "warn", "Degraded", summary
    return "ok", "Ready", summary


def job_health(o):
    st, spec = o.get("status") or {}, o.get("spec") or {}
    conds = cond_map(st)
    f = conds.get("Failed")
    if f and f.get("status") == "True":
        return "fail", f.get("reason") or "Failed", f"{st.get('failed', 0)} failed pods; {(f.get('message') or '')[:200]}"
    c = conds.get("Complete")
    if c and c.get("status") == "True":
        return "ok", "Complete", f"{st.get('succeeded', 0)} succeeded"
    if spec.get("suspend"):
        return "ok", "Suspended", "suspended"
    return "ok", "Running", f"{st.get('active', 0)} active, {st.get('failed', 0)} failed"


def node_health(o):
    st, spec = o.get("status") or {}, o.get("spec") or {}
    conds = cond_map(st)
    alloc = st.get("allocatable") or {}
    summary = f"allocatable cpu {fcpu(cpu(alloc.get('cpu')))} mem {fmem(mem(alloc.get('memory')))}"
    r = conds.get("Ready")
    if not r or r.get("status") != "True":
        return "fail", "NodeNotReady", f"Ready={r.get('status') if r else '?'} {(r or {}).get('message', '')[:200]}"
    for p in ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable"):
        if (conds.get(p) or {}).get("status") == "True":
            return "fail", p, (conds[p].get("message") or p)[:200]
    if spec.get("unschedulable"):
        return "warn", "Cordoned", "unschedulable (cordoned): no new pods land here"
    return "ok", "Ready", summary


def pvc_health(o):
    st = o.get("status") or {}
    phase = st.get("phase", "Pending")
    spec = o.get("spec") or {}
    size = ((spec.get("resources") or {}).get("requests") or {}).get("storage", "?")
    summary = f"{phase} {size} class {spec.get('storageClassName', '-')}"
    if phase == "Bound":
        return "ok", "Bound", summary
    if phase == "Lost":
        return "fail", "Lost", "its PersistentVolume is gone"
    return "fail", "Pending", summary


def pv_health(o):
    st, spec = o.get("status") or {}, o.get("spec") or {}
    phase = st.get("phase", "?")
    summary = f"{phase} {(spec.get('capacity') or {}).get('storage', '?')} {spec.get('persistentVolumeReclaimPolicy', '')}"
    if phase == "Failed":
        return "fail", "Failed", (st.get("message") or summary)[:200]
    if phase == "Released":
        return "warn", "Released", f"{summary}: claim deleted, the disk is kept (and billed) until the PV is removed"
    return "ok", phase, summary


def hpa_health(o):
    st, spec = o.get("status") or {}, o.get("spec") or {}
    summary = f"{st.get('currentReplicas', '?')}->{st.get('desiredReplicas', '?')} replicas (min {spec.get('minReplicas', 1)} max {spec.get('maxReplicas', '?')})"
    conds = cond_map(st)
    if not conds:
        raw = ((o.get("metadata") or {}).get("annotations") or {}).get("autoscaling.alpha.kubernetes.io/conditions")
        if raw:
            try:
                conds = {c.get("type"): c for c in json.loads(raw)}
            except ValueError:
                pass
    for t in ("AbleToScale", "ScalingActive"):
        c = conds.get(t)
        if c and c.get("status") == "False":
            return "fail", c.get("reason") or t, f"{summary}; {(c.get('message') or '')[:200]}"
    lim = conds.get("ScalingLimited")
    if lim and lim.get("status") == "True" and lim.get("reason") == "TooManyReplicas":
        return "warn", "AtMaxReplicas", f"{summary}; {(lim.get('message') or '')[:150]}"
    return "ok", "Active", summary


def apiservice_health(o):
    c = cond_map(o.get("status")).get("Available")
    if (o.get("spec") or {}).get("service") is None:
        return "ok", "Local", "served by kube-apiserver"
    if c and c.get("status") != "True":
        return "fail", c.get("reason") or "Unavailable", (c.get("message") or "")[:250]
    return "ok", "Available", "available"


def crd_health(o):
    conds = cond_map(o.get("status"))
    for t in ("Established", "NamesAccepted"):
        c = conds.get(t)
        if c and c.get("status") == "False":
            return "fail", c.get("reason") or t, (c.get("message") or "")[:200]
    return "ok", "Established", f"{(o.get('spec') or {}).get('group')} {((o.get('spec') or {}).get('names') or {}).get('kind')}"


def quota_health(o):
    st = o.get("status") or {}
    hard, used = st.get("hard") or {}, st.get("used") or {}
    full = []
    for k, h in hard.items():
        u = used.get(k)
        if u is None:
            continue
        conv = mem if any(x in k for x in ("memory", "storage")) else cpu
        try:
            if conv(h) > 0 and conv(u) >= conv(h):
                full.append(f"{k} {u}/{h}")
        except ValueError:
            continue
    if full:
        return "warn", "QuotaExhausted", "used up: " + ", ".join(full)
    return "ok", "Within", ", ".join(f"{k} {used.get(k, '0')}/{v}" for k, v in list(hard.items())[:4])


def namespace_health(o, now):
    md, st = o.get("metadata") or {}, o.get("status") or {}
    if st.get("phase") == "Terminating" and md.get("deletionTimestamp"):
        late = now - (ts(md["deletionTimestamp"]) or now)
        if late > 300:
            msgs = [c.get("message", "") for c in st.get("conditions") or [] if c.get("status") == "True"]
            return "warn", "StuckTerminating", f"terminating for {ago(late)}: {'; '.join(msgs)[:250]}"
    return "-", st.get("phase", "Active"), st.get("phase", "Active")


GOOD_TRUE = ("Ready", "Available", "Healthy", "Synced", "Reconciled", "Established", "Succeeded")
BAD_TRUE = ("Degraded", "Failed", "Stalled", "Error")


def generic_health(o):
    """Custom resources and anything else: status.conditions, phase/state, Argo CD health/sync,
    and observedGeneration lag. Objects with none of these have no health ("-")."""
    st = o.get("status")
    if not isinstance(st, dict) or not st:
        return "-", "", ""
    conds = cond_map(st)
    for t in GOOD_TRUE:
        c = conds.get(t)
        if c and c.get("status") == "False":
            return "fail", f"{t}=False" + (f" {c['reason']}" if c.get("reason") else ""), (c.get("message") or "")[:250]
    for t in BAD_TRUE:
        c = conds.get(t)
        if c and c.get("status") == "True":
            return "fail", f"{t}" + (f" {c['reason']}" if c.get("reason") else ""), (c.get("message") or "")[:250]
    phase = st.get("phase") or st.get("state")
    if isinstance(phase, str) and phase.lower() in ("failed", "error", "degraded", "crashloopbackoff"):
        return "fail", phase, (st.get("message") or st.get("reason") or "")[:250] if isinstance(st.get("message", ""), str) else ""
    health = (st.get("health") or {}).get("status") if isinstance(st.get("health"), dict) else None
    if health in ("Degraded", "Missing"):
        return "fail", f"health {health}", ((st.get("health") or {}).get("message") or "")[:250]
    sync = (st.get("sync") or {}).get("status") if isinstance(st.get("sync"), dict) else None
    if sync == "OutOfSync" or health == "Progressing":
        return "warn", f"{'OutOfSync' if sync == 'OutOfSync' else 'Progressing'}", f"sync {sync}, health {health}"
    gen, obs = (o.get("metadata") or {}).get("generation"), st.get("observedGeneration")
    if isinstance(gen, int) and isinstance(obs, int) and obs < gen:
        return "warn", "NotReconciled", f"generation {gen}, controller has seen {obs}"
    for t in ("Reconciling", "Progressing"):
        c = conds.get(t)
        if c and c.get("status") == "True" and t == "Reconciling":
            return "warn", "Reconciling", (c.get("message") or "")[:200]
    if conds or phase or health:
        s = ", ".join(f"{t}={c.get('status')}" for t, c in list(conds.items())[:3]) or f"{phase or health}"
        return "ok", "Ready", s
    return "-", "", ""


def own_health(kind, o, now):
    if kind == "Pod":
        return pod_health(o, now)
    if kind in ("Deployment", "ReplicaSet", "StatefulSet", "ReplicationController"):
        h = replicas_health(kind, o)
        if kind == "ReplicaSet" and (o.get("spec") or {}).get("replicas", 1) == 0:
            return "-", "OldRevision", "0 replicas"
        return h
    if kind == "DaemonSet":
        return daemonset_health(o)
    if kind == "Job":
        return job_health(o)
    if kind == "CronJob":
        spec = o.get("spec") or {}
        last = (o.get("status") or {}).get("lastScheduleTime")
        return "ok", "Suspended" if spec.get("suspend") else "Scheduled", f"schedule '{spec.get('schedule')}', last run {last or 'never'}"
    if kind == "Node":
        return node_health(o)
    if kind == "PersistentVolumeClaim":
        return pvc_health(o)
    if kind == "PersistentVolume":
        return pv_health(o)
    if kind == "HorizontalPodAutoscaler":
        return hpa_health(o)
    if kind == "APIService":
        return apiservice_health(o)
    if kind == "CustomResourceDefinition":
        return crd_health(o)
    if kind == "ResourceQuota":
        return quota_health(o)
    if kind == "Namespace":
        return namespace_health(o, now)
    if kind in ("Service", "Ingress", "ValidatingWebhookConfiguration", "MutatingWebhookConfiguration"):
        return "-", "", ""   # relational: set in link()
    if kind in ("ConfigMap", "Secret", "ServiceAccount"):
        keys = o.get("dataKeys")
        return "-", "", f"keys: {', '.join(keys[:8])}{' ...' if len(keys) > 8 else ''}" if keys is not None else ""
    return generic_health(o)


# --------------------------------------------------------------------------- building the graph

class Builder:
    def __init__(self, resources, now):
        self.now = now
        self.resources = resources                 # api-resources rows (may be [] offline)
        self.objs = {}                             # id -> sanitized object
        self.meta = {}                             # id -> [kind, group, ns, name]
        self.health = {}                           # id -> (health, reason, summary)
        self.edges = set()                         # (src, dst, type, detail)
        self.events = []                           # (obj, type, reason, message, count, last)
        self.by_uid = {}
        self.usage = {}                            # id -> (cpu cores, mem bytes)
        kinds = {}
        for r in resources:
            kinds.setdefault(r["kind"], set()).add(r["group"])
        self.kind_groups = kinds

    def tok(self, kind, group):
        groups = self.kind_groups.get(kind)
        if groups and len(groups) > 1 and group:
            return f"{kind}.{group}"
        return kind

    def oid(self, kind, group, ns, name):
        t = self.tok(kind, group)
        return f"{t}/{ns}/{name}" if ns else f"{t}/{name}"

    def ref(self, kind, ns, name, group=None):
        """Id of an object referred to by kind/ns/name, existing or not."""
        if group is None:
            groups = self.kind_groups.get(kind) or {""}
            group = sorted(groups)[0]
        return self.oid(kind, group, ns, name)

    def add_items(self, items):
        for o in items:
            kind = o.get("kind")
            if not kind or kind in ("Event",):
                continue
            group = (o.get("apiVersion") or "").split("/")[0] if "/" in (o.get("apiVersion") or "") else ""
            self.kind_groups.setdefault(kind, set()).add(group)
        for o in items:
            kind = o.get("kind")
            if not kind:
                continue
            if kind == "Event":
                self.add_event(o)
                continue
            if kind in ("PodMetrics", "NodeMetrics"):
                self.add_usage(o)
                continue
            md = o.get("metadata") or {}
            group = (o.get("apiVersion") or "").split("/")[0] if "/" in (o.get("apiVersion") or "") else ""
            i = self.oid(kind, group, md.get("namespace", ""), md.get("name", ""))
            self.objs[i] = sanitize(o)
            self.meta[i] = [kind, group, md.get("namespace", ""), md.get("name", "")]
            if md.get("uid"):
                self.by_uid[md["uid"]] = i

    def add_event(self, e):
        inv = e.get("involvedObject") or e.get("regarding") or {}
        last = (e.get("series") or {}).get("lastObservedTime") or e.get("lastTimestamp") or e.get("eventTime") \
            or (e.get("metadata") or {}).get("creationTimestamp")
        self.events.append([inv, e.get("type", "Normal"), e.get("reason", ""), (e.get("message") or e.get("note") or "").strip(),
                            e.get("count") or (e.get("series") or {}).get("count") or 1, ts(last) or 0])

    def add_usage(self, m):
        md = m.get("metadata") or {}
        if m.get("kind") == "NodeMetrics":
            u = m.get("usage") or {}
            self.usage[("Node", "", md.get("name"))] = (cpu(u.get("cpu")), mem(u.get("memory")))
        else:
            c = sum(cpu((x.get("usage") or {}).get("cpu")) for x in m.get("containers") or [])
            b = sum(mem((x.get("usage") or {}).get("memory")) for x in m.get("containers") or [])
            self.usage[("Pod", md.get("namespace"), md.get("name"))] = (c, b)

    def edge(self, src, dst, etype, detail=""):
        if src != dst:
            self.edges.add((src, dst, etype, detail))

    def need(self, src, kind, ns, name, etype, detail="", optional=False):
        """Edge to a referenced object; a missing, non-optional target becomes a 'missing' node."""
        if not name:
            return
        dst = self.ref(kind, ns, name)
        if dst not in self.objs:
            if optional:
                return
            self.meta.setdefault(dst, [kind, "", ns, name])
            self.health[dst] = ("fail", "missing", f"referenced by {src} but not found")
        self.edge(src, dst, etype, detail)

    def link(self):
        now = self.now
        for i, o in self.objs.items():
            self.health[i] = own_health(self.meta[i][0], o, now)
        # usage onto pods and nodes
        for i, (kind, _g, ns, name) in list(self.meta.items()):
            if kind in ("Pod", "Node") and (kind, ns, name) in self.usage:
                self.objs[i]["_usage"] = self.usage[(kind, ns, name)]
        pods_by_ns = {}
        for i, (kind, _g, ns, _n) in self.meta.items():
            if kind == "Pod" and i in self.objs:
                pods_by_ns.setdefault(ns, []).append(i)
        crd_by_kind = {}
        for i, (kind, _g, _ns, _n) in self.meta.items():
            if kind == "CustomResourceDefinition" and i in self.objs:
                spec = self.objs[i].get("spec") or {}
                crd_by_kind[(spec.get("group"), (spec.get("names") or {}).get("kind"))] = i
        for i in list(self.objs):
            o = self.objs[i]
            kind, group, ns, name = self.meta[i]
            md, spec = o.get("metadata") or {}, o.get("spec") or {}
            for ow in md.get("ownerReferences") or []:
                owner = self.by_uid.get(ow.get("uid")) or self.ref(ow.get("kind"), ns, ow.get("name"))
                if owner in self.objs:
                    self.edge(owner, i, "owns")
            if (group, kind) in crd_by_kind:
                self.edge(i, crd_by_kind[(group, kind)], "instance-of")
            if kind == "Pod":
                self.link_podspec(i, ns, spec)
                if spec.get("nodeName"):
                    self.need(i, "Node", "", spec["nodeName"], "runs-on")
            elif kind in ("Deployment", "StatefulSet", "DaemonSet", "Job", "ReplicaSet", "ReplicationController"):
                self.link_podspec(i, ns, ((spec.get("template") or {}).get("spec") or {}))
            elif kind == "CronJob":
                self.link_podspec(i, ns, (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec") or {})
            elif kind == "Service":
                self.link_service(i, ns, spec, pods_by_ns.get(ns, []))
            elif kind == "Ingress":
                self.link_ingress(i, ns, spec)
            elif kind == "HorizontalPodAutoscaler":
                t = spec.get("scaleTargetRef") or {}
                self.need(i, t.get("kind"), ns, t.get("name"), "scales")
            elif kind == "PersistentVolumeClaim":
                if spec.get("volumeName"):
                    self.need(i, "PersistentVolume", "", spec["volumeName"], "bound-to")
                if spec.get("storageClassName"):
                    self.need(i, "StorageClass", "", spec["storageClassName"], "uses", "storageClassName")
            elif kind == "PersistentVolume":
                if spec.get("storageClassName"):
                    self.need(i, "StorageClass", "", spec["storageClassName"], "uses", "storageClassName", optional=True)
            elif kind == "APIService":
                svc = spec.get("service")
                if svc:
                    self.need(i, "Service", svc.get("namespace"), svc.get("name"), "calls")
            elif kind in ("RoleBinding", "ClusterRoleBinding"):
                rr = o.get("roleRef") or {}
                self.need(i, rr.get("kind"), ns if rr.get("kind") == "Role" else "", rr.get("name"), "grants", optional=True)
                for s in o.get("subjects") or []:
                    if s.get("kind") == "ServiceAccount":
                        self.need(i, "ServiceAccount", s.get("namespace") or ns, s.get("name"), "binds", optional=True)
            elif kind in ("PodDisruptionBudget", "NetworkPolicy"):
                sel = spec.get("selector") if kind == "PodDisruptionBudget" else spec.get("podSelector")
                for p in pods_by_ns.get(ns, []):
                    if selects(sel, self.objs[p]):
                        self.edge(i, p, "selects")
            elif kind == "StatefulSet" and spec.get("serviceName"):
                self.need(i, "Service", ns, spec["serviceName"], "uses", "serviceName", optional=True)
            elif kind not in BUILTIN_KINDS:
                self.link_generic(i, ns, spec)
        for i in list(self.objs):
            kind, _g, ns, _n = self.meta[i]
            if kind in ("ValidatingWebhookConfiguration", "MutatingWebhookConfiguration"):
                self.link_webhooks(i, self.objs[i])
        self.link_ingress_health()
        self.attach_events()
        self.event_health()
        self.rollout_health()
        self.node_requests()

    def link_podspec(self, i, ns, spec):
        for v in spec.get("volumes") or []:
            vn = v.get("name")
            if v.get("configMap"):
                self.need(i, "ConfigMap", ns, v["configMap"].get("name"), "uses", f"volume {vn}", v["configMap"].get("optional", False))
            if v.get("secret"):
                self.need(i, "Secret", ns, v["secret"].get("secretName"), "uses", f"volume {vn}", v["secret"].get("optional", False))
            if v.get("persistentVolumeClaim"):
                self.need(i, "PersistentVolumeClaim", ns, v["persistentVolumeClaim"].get("claimName"), "uses", f"volume {vn}")
            for src in (v.get("projected") or {}).get("sources") or []:
                if src.get("configMap"):
                    self.need(i, "ConfigMap", ns, src["configMap"].get("name"), "uses", f"volume {vn}", src["configMap"].get("optional", False))
                if src.get("secret"):
                    self.need(i, "Secret", ns, src["secret"].get("name"), "uses", f"volume {vn}", src["secret"].get("optional", False))
        for c in (spec.get("containers") or []) + (spec.get("initContainers") or []):
            for ef in c.get("envFrom") or []:
                if ef.get("configMapRef"):
                    self.need(i, "ConfigMap", ns, ef["configMapRef"].get("name"), "uses", f"envFrom ({c.get('name')})", ef["configMapRef"].get("optional", False))
                if ef.get("secretRef"):
                    self.need(i, "Secret", ns, ef["secretRef"].get("name"), "uses", f"envFrom ({c.get('name')})", ef["secretRef"].get("optional", False))
            for e in c.get("env") or []:
                vf = e.get("valueFrom") or {}
                for key, kind in (("configMapKeyRef", "ConfigMap"), ("secretKeyRef", "Secret")):
                    r = vf.get(key)
                    if r:
                        self.need(i, kind, ns, r.get("name"), "uses", f"env {e.get('name')} key {r.get('key')} ({c.get('name')})", r.get("optional", False))
                        dst = self.ref(kind, ns, r.get("name"))
                        keys = (self.objs.get(dst) or {}).get("dataKeys")
                        if keys is not None and r.get("key") not in keys and not r.get("optional"):
                            self.health[dst] = ("fail", "missing key", f"key {r.get('key')} not in {kind} (needed by {i}, env {e.get('name')})")
        sa = spec.get("serviceAccountName") or spec.get("serviceAccount")
        if sa:
            self.need(i, "ServiceAccount", ns, sa, "uses", "serviceAccountName")
        for s in spec.get("imagePullSecrets") or []:
            self.need(i, "Secret", ns, s.get("name"), "uses", "imagePullSecrets", optional=True)
        if spec.get("priorityClassName"):
            self.need(i, "PriorityClass", "", spec["priorityClassName"], "uses", "priorityClassName")

    def link_service(self, i, ns, spec, pods):
        sel = spec.get("selector")
        if not sel or spec.get("type") == "ExternalName":
            self.health[i] = ("-", "", f"{spec.get('type', 'ClusterIP')} without selector")
            return
        live = [p for p in pods if (self.objs[p].get("status") or {}).get("phase") not in ("Succeeded", "Failed")
                and selects({"matchLabels": sel}, self.objs[p])]
        for p in live:
            self.edge(i, p, "selects")
        ready = [p for p in live if cond_map(self.objs[p].get("status")).get("Ready", {}).get("status") == "True"]
        sel_s = ",".join(f"{k}={v}" for k, v in sel.items())
        ports = ",".join(str(p.get("port")) for p in spec.get("ports") or [])
        if not live:
            self.health[i] = ("fail", "NoPods", f"selector {sel_s} matches no pod")
        elif not ready:
            self.health[i] = ("fail", "NoReadyEndpoints", f"{len(live)} pods match {sel_s}, none Ready")
        elif spec.get("type") == "LoadBalancer" and not ((self.objs[i].get("status") or {}).get("loadBalancer") or {}).get("ingress"):
            self.health[i] = ("warn", "LoadBalancerPending", f"{len(ready)}/{len(live)} ready endpoints, no external address yet")
        else:
            self.health[i] = ("ok", "Endpoints", f"{len(ready)}/{len(live)} ready endpoints, ports {ports}")

    def link_ingress(self, i, ns, spec):
        backs = []
        if spec.get("defaultBackend"):
            backs.append(spec["defaultBackend"])
        if spec.get("backend"):
            backs.append(spec["backend"])
        for r in spec.get("rules") or []:
            for p in ((r.get("http") or {}).get("paths")) or []:
                backs.append(p.get("backend") or {})
        for b in backs:
            svc = (b.get("service") or {}).get("name") or b.get("serviceName")
            if svc:
                self.need(i, "Service", ns, svc, "routes-to")
        for t in spec.get("tls") or []:
            if t.get("secretName"):
                self.need(i, "Secret", ns, t["secretName"], "uses", "tls")
        if spec.get("ingressClassName"):
            self.need(i, "IngressClass", "", spec["ingressClassName"], "uses", "ingressClassName", optional=True)

    def link_ingress_health(self):
        out = {}
        for s, d, t, _ in self.edges:
            out.setdefault(s, []).append((d, t))
        for i, (kind, *_r) in self.meta.items():
            if kind != "Ingress" or i not in self.objs:
                continue
            bad = [d for d, t in out.get(i, []) if t in ("routes-to", "uses") and self.health.get(d, ("-",))[0] == "fail"]
            if bad:
                self.health[i] = ("fail", "BackendUnavailable", "; ".join(f"{d}: {self.health[d][1]}" for d in bad)[:250])
            else:
                n = sum(1 for _d, t in out.get(i, []) if t == "routes-to")
                self.health[i] = ("ok", "Routed", f"{n} backend services")

    def link_webhooks(self, i, o):
        problems, fail = [], False
        for w in o.get("webhooks") or []:
            svc = (w.get("clientConfig") or {}).get("service")
            if not svc:
                continue
            dst = self.ref("Service", svc.get("namespace"), svc.get("name"))
            self.need(i, "Service", svc.get("namespace"), svc.get("name"), "calls", f"webhook {w.get('name')}")
            h = self.health.get(dst, ("-", ""))
            if h[0] == "fail":
                blocking = w.get("failurePolicy", "Fail") == "Fail"
                fail = fail or blocking
                problems.append(f"webhook {w.get('name')} -> {dst} {h[1]} (failurePolicy {w.get('failurePolicy', 'Fail')}"
                                f"{': blocks admission of matched objects' if blocking else ''})")
        if problems:
            self.health[i] = ("fail" if fail else "warn", "WebhookUnavailable", "; ".join(problems)[:300])
        else:
            self.health[i] = ("ok", "Served", f"{len(o.get('webhooks') or [])} webhooks")

    def link_generic(self, i, ns, spec):
        """Custom resources: follow common reference shapes, only to objects that exist."""
        def walk(node, path):
            if isinstance(node, dict):
                for k, v in node.items():
                    p = f"{path}.{k}" if path else k
                    if isinstance(v, str) and ns:
                        kind = {"secretName": "Secret", "configMapName": "ConfigMap", "serviceName": "Service",
                                "claimName": "PersistentVolumeClaim", "serviceAccountName": "ServiceAccount"}.get(k)
                        if kind:
                            dst = self.ref(kind, ns, v)
                            if dst in self.objs:
                                self.edge(i, dst, "uses", f"spec.{p}")
                    elif isinstance(v, dict) and k in ("secretRef", "configMapRef", "serviceRef", "tokenSecretRef") \
                            and isinstance(v.get("name"), str) and ns:
                        kind = {"secretRef": "Secret", "tokenSecretRef": "Secret", "configMapRef": "ConfigMap",
                                "serviceRef": "Service"}[k]
                        dst = self.ref(kind, v.get("namespace") or ns, v["name"])
                        if dst in self.objs:
                            self.edge(i, dst, "uses", f"spec.{p}")
                    else:
                        walk(v, p)
            elif isinstance(node, list):
                for n, v in enumerate(node[:50]):
                    walk(v, f"{path}[{n}]")
        walk(spec, "")

    def attach_events(self):
        agg = {}
        for inv, etype, reason, msg, count, last in self.events:
            i = self.by_uid.get(inv.get("uid")) or self.ref(inv.get("kind") or "?", inv.get("namespace") or "", inv.get("name") or "?")
            norm = re.sub(r"\b[0-9a-f]{8,}\b|\d+", "#", msg)[:160]
            k = (i, etype, reason, norm)
            a = agg.setdefault(k, [i, etype, reason, msg, 0, 0])
            a[4] += count or 1
            if last >= a[5]:
                a[5], a[3] = last, msg
        self.events = list(agg.values())

    def event_health(self):
        """Refine reasons from events: probe failures, scale-up hopes, PVC provisioning errors."""
        ev = {}
        for e in self.events:
            ev.setdefault(e[0], []).append(e)
        for i, (kind, *_r) in self.meta.items():
            h = self.health.get(i)
            if not h or i not in self.objs:
                continue
            reasons = {e[2]: e for e in ev.get(i, [])}
            if kind == "Pod" and h[1] in ("NotReady", "Starting") and "Unhealthy" in reasons \
                    and (h[1] == "NotReady" or reasons["Unhealthy"][4] >= 3):
                self.health[i] = ("fail", "ProbeFailing", reasons["Unhealthy"][3][:250])
            elif kind == "Pod" and h[1] == "Unschedulable" and "TriggeredScaleUp" in reasons:
                self.health[i] = ("warn", "WaitingForScaleUp", reasons["TriggeredScaleUp"][3][:250])
            elif kind == "Pod" and h[1] == "ContainerCreating":
                for r in ("FailedMount", "FailedAttachVolume", "FailedCreatePodSandBox"):
                    if r in reasons:
                        self.health[i] = ("fail", r, reasons[r][3][:250])
                        break
            elif kind == "PersistentVolumeClaim" and h[0] == "fail":
                for r in ("ProvisioningFailed", "FailedBinding", "ExternalProvisioning", "WaitForFirstConsumer"):
                    if r in reasons:
                        sev = "ok" if r == "WaitForFirstConsumer" else "fail"
                        self.health[i] = (sev, r, reasons[r][3][:250])
                        break

    def rollout_health(self):
        """A Deployment whose newest ReplicaSet fails while an older one still serves: the rollout is stuck
        (the Service keeps working on the old revision, so this hides behind a green dashboard)."""
        kids = {}
        for s, d, t, _ in self.edges:
            if t == "owns":
                kids.setdefault(s, []).append(d)
        for i, (kind, *_r) in self.meta.items():
            if kind != "Deployment" or i not in self.objs:
                continue
            live = []
            for rs in kids.get(i, []):
                o = self.objs.get(rs) or {}
                if self.meta[rs][0] == "ReplicaSet" and (o.get("spec") or {}).get("replicas", 0) > 0:
                    rev = ((o.get("metadata") or {}).get("annotations") or {}).get("deployment.kubernetes.io/revision", "0")
                    live.append((int(rev) if rev.isdigit() else 0, rs, o))
            if len(live) < 2:
                continue
            live.sort()
            (new_rev, new_rs, new_o), old = live[-1], live[:-1]
            serving = sum(((o.get("status") or {}).get("readyReplicas") or 0) for _r, _x, o in old)
            bad = [p for p in kids.get(new_rs, []) if self.health.get(p, ("-",))[0] == "fail"]
            if not serving or not bad:
                continue
            h, r, s = self.health[i]
            want = (new_o.get("spec") or {}).get("replicas", 0)
            ready = (new_o.get("status") or {}).get("readyReplicas") or 0
            pod_r = self.health[bad[0]][1]
            deadline = " (progress deadline exceeded)" if r == "ProgressDeadlineExceeded" else ""
            self.health[i] = ("fail" if h == "fail" else "warn", "RolloutStuck",
                              f"revision {new_rev} {ready}/{want} ready, its pods {pod_r}; revision "
                              f"{','.join(str(x[0]) for x in old)} still serving {serving} pods{deadline}")

    def node_requests(self):
        req = {}
        for i, (kind, _g, _ns, _n) in self.meta.items():
            o = self.objs.get(i)
            if kind != "Pod" or not o or (o.get("status") or {}).get("phase") in ("Succeeded", "Failed"):
                continue
            node = (o.get("spec") or {}).get("nodeName")
            if node:
                c, m = pod_requests(o.get("spec") or {})
                r = req.setdefault(node, [0.0, 0.0])
                r[0] += c; r[1] += m
        for i, (kind, _g, _ns, name) in self.meta.items():
            if kind == "Node" and i in self.objs:
                self.objs[i]["_requests"] = req.get(name, [0.0, 0.0])

    def write(self, path, meta):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp{os.getpid()}"
        if os.path.exists(tmp):
            os.remove(tmp)
        db = sqlite3.connect(tmp)
        db.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE nodes(id TEXT PRIMARY KEY, kind TEXT, grp TEXT, ns TEXT, name TEXT, health TEXT,
                               reason TEXT, summary TEXT, created REAL, raw TEXT);
            CREATE TABLE edges(src TEXT, dst TEXT, type TEXT, detail TEXT);
            CREATE TABLE events(obj TEXT, type TEXT, reason TEXT, message TEXT, count INTEGER, last REAL);
            CREATE TABLE kinds(kind TEXT, grp TEXT, resource TEXT, namespaced INTEGER, short TEXT);
        """)
        rows = []
        for i, (kind, group, ns, name) in self.meta.items():
            o = self.objs.get(i)
            h = self.health.get(i, ("-", "", ""))
            created = ts(((o or {}).get("metadata") or {}).get("creationTimestamp"))
            rows.append((i, kind, group, ns, name, h[0], h[1], h[2], created, json.dumps(o, separators=(",", ":")) if o else None))
        db.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        db.executemany("INSERT INTO edges VALUES (?,?,?,?)", sorted(self.edges))
        db.executemany("INSERT INTO events VALUES (?,?,?,?,?,?)", self.events)
        db.executemany("INSERT INTO kinds VALUES (?,?,?,?,?)",
                       [(r["kind"], r["group"], r["name"], int(r["namespaced"]), ",".join(r["short"])) for r in self.resources])
        db.executemany("INSERT INTO meta VALUES (?,?)", [(k, json.dumps(v)) for k, v in meta.items()])
        db.executescript("""
            CREATE INDEX e_src ON edges(src); CREATE INDEX e_dst ON edges(dst);
            CREATE INDEX n_name ON nodes(name); CREATE INDEX n_ns ON nodes(ns); CREATE INDEX ev_obj ON events(obj);
        """)
        db.commit()
        db.close()
        os.replace(tmp, path)


BUILTIN_KINDS = {"Pod", "Deployment", "ReplicaSet", "StatefulSet", "DaemonSet", "Job", "CronJob", "Service", "Ingress",
                 "ConfigMap", "Secret", "ServiceAccount", "Node", "Namespace", "PersistentVolume",
                 "PersistentVolumeClaim", "StorageClass", "HorizontalPodAutoscaler", "APIService",
                 "CustomResourceDefinition", "ResourceQuota", "LimitRange", "Role", "ClusterRole", "RoleBinding",
                 "ClusterRoleBinding", "PodDisruptionBudget", "NetworkPolicy", "PriorityClass", "IngressClass",
                 "ValidatingWebhookConfiguration", "MutatingWebhookConfiguration", "ReplicationController",
                 "CSIDriver", "CSINode", "VolumeAttachment", "RuntimeClass", "CertificateSigningRequest",
                 "FlowSchema", "PriorityLevelConfiguration", "ValidatingAdmissionPolicy",
                 "ValidatingAdmissionPolicyBinding"}


def selects(sel, pod):
    """LabelSelector (matchLabels + matchExpressions) against a pod; an empty selector selects all."""
    if sel is None:
        return False
    labels = ((pod.get("metadata") or {}).get("labels")) or {}
    for k, v in (sel.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for e in sel.get("matchExpressions") or []:
        k, op, vals = e.get("key"), e.get("operator"), e.get("values") or []
        if op == "In" and labels.get(k) not in vals or op == "NotIn" and labels.get(k) in vals \
                or op == "Exists" and k not in labels or op == "DoesNotExist" and k in labels:
            return False
    return True


# --------------------------------------------------------------------------- snapshot

def db_path(args, ctx):
    if getattr(args, "db", None):
        return args.db
    return os.path.join(DB_DIR, re.sub(r"[^A-Za-z0-9_.@-]+", "_", ctx) + ".db")


def collect(ctx, namespaces, include, exclude, timeout, jobs):
    rc, out, err = kubectl(ctx, ["api-resources", "--verbs=list", "-o", "wide"], timeout=timeout)
    if rc != 0 and not out:
        raise SystemExit(f"kubegraph: cannot reach the cluster of context {ctx}: {err.strip()[:300]}")
    resources = parse_api_resources(out)
    # an unavailable aggregated API (APIService) makes discovery partial: name the groups, once
    groups = sorted(set(re.findall(r"couldn't get resource list for ([^:\s]+)", err)))
    errors = [f"API group {g}: unavailable, its resources were not listed" for g in groups]
    want = []
    for r in resources:
        q = qname(r)
        if (q in SKIP and q not in include) or q in exclude or r["name"] in exclude:
            continue
        want.append(r)

    def fetch(r):
        q = qname(r)
        if r["namespaced"] and namespaces:
            items, errs = [], []
            for ns in namespaces:
                rc, out, err = kubectl(ctx, ["get", q, "-n", ns, "-o", "json", f"--request-timeout={timeout}s"], timeout + 10)
                if rc == 0:
                    items += json.loads(out).get("items") or []
                else:
                    errs.append((clean_err(err) or [f"exit {rc}"])[-1])
            return r, items, errs
        args = ["get", q, "-o", "json", f"--request-timeout={timeout}s"] + (["-A"] if r["namespaced"] else [])
        rc, out, err = kubectl(ctx, args, timeout + 10)
        if rc != 0:
            return r, [], [(clean_err(err) or [f"exit {rc}"])[-1]]
        try:
            return r, json.loads(out).get("items") or [], []
        except ValueError as e:
            return r, [], [f"bad JSON: {e}"]

    lists = {}
    with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
        for r, items, errs in ex.map(fetch, want):
            for it in items:
                it.setdefault("kind", r["kind"])
                if "apiVersion" not in it and r["group"]:
                    it["apiVersion"] = f"{r['group']}/v1"
            lists[qname(r)] = items
            errors += [f"{qname(r)}: {e}" for e in errs]
    rc, out, _ = kubectl(ctx, ["version", "-o", "json"], timeout=timeout)
    server = ""
    if out:
        try:
            server = (json.loads(out).get("serverVersion") or {}).get("gitVersion", "")
        except ValueError:
            pass
    return resources, lists, errors, server


def cmd_snapshot(args, quiet=False):
    t0 = time.time()
    if args.from_dir:
        resources, lists, meta_in = load_dir(args.from_dir)
        ctx = args.context or meta_in.get("context") or "offline"
        now = meta_in.get("taken_at") or t0
        errors, server = meta_in.get("errors", []), meta_in.get("server", "")
        source = f"from {args.from_dir}"
    else:
        ctx = args.context or current_context()
        include, exclude = set(args.include or []), set(args.exclude or [])
        resources, lists, errors, server = collect(ctx, args.namespace or [], include, exclude, args.timeout, args.jobs)
        now = time.time()
        source = "live"
    b = Builder(resources, now)
    for q in sorted(lists):
        b.add_items(lists[q])
    b.link()
    meta = {"context": ctx, "taken_at": now, "server": server, "errors": errors, "source": source,
            "namespaces": args.namespace or [], "include": args.include or [], "exclude": args.exclude or [],
            "version": VERSION}
    path = db_path(args, ctx)
    b.write(path, meta)
    if args.save:
        save_dir(args.save, resources, lists, meta)
    if quiet:
        return path
    counts = {}
    for h in b.health.values():
        counts[h[0]] = counts.get(h[0], 0) + 1
    kinds = len({m[0] for m in b.meta.values()})
    print(f"snapshot {ctx} ({server or 'server ?'}): {len(b.objs)} objects, {kinds} kinds, "
          f"{counts.get('fail', 0)} fail, {counts.get('warn', 0)} warn, {len(b.events)} event groups "
          f"in {time.time() - t0:.1f}s -> {path}")
    for e in errors[:10]:
        print(f"  not collected: {e}")
    if len(errors) > 10:
        print(f"  ... {len(errors) - 10} more not collected")
    return path


def save_dir(d, resources, lists, meta):
    """Sanitized lists (one file per resource type) + api-resources + meta: input for --from."""
    os.makedirs(d, exist_ok=True)
    for q, items in lists.items():
        if not items:
            continue
        for it in items:
            sanitize(it)
        with open(os.path.join(d, f"{q}.json"), "w") as f:   # one object per line: compact, still diffable
            f.write('{"apiVersion": "v1", "kind": "List", "items": [\n')
            f.write(",\n".join(json.dumps(it, sort_keys=True, separators=(",", ":")) for it in items))
            f.write("\n]}\n")
    with open(os.path.join(d, "api-resources.json"), "w") as f:
        json.dump(resources, f, indent=1)
    with open(os.path.join(d, "meta.json"), "w") as f:
        json.dump({k: meta[k] for k in ("context", "taken_at", "server", "errors")}, f, indent=1)


def load_dir(d):
    lists, resources, meta = {}, [], {}
    for fn in sorted(os.listdir(d)):
        p = os.path.join(d, fn)
        if fn == "api-resources.json":
            resources = json.load(open(p))
        elif fn == "meta.json":
            meta = json.load(open(p))
        elif fn == "api-resources.txt":
            resources = parse_api_resources(open(p).read())
        elif fn.endswith(".json"):
            data = json.load(open(p))
            lists[fn[:-5]] = data.get("items", [data] if data.get("kind") != "List" else [])
    return resources, lists, meta


# --------------------------------------------------------------------------- reading the graph

class Graph:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.meta = {k: json.loads(v) for k, v in self.db.execute("SELECT key, value FROM meta")}
        self.nodes = {}
        for row in self.db.execute("SELECT id, kind, grp, ns, name, health, reason, summary, created FROM nodes"):
            self.nodes[row[0]] = row
        self.out, self.inn = {}, {}
        for s, d, t, det in self.db.execute("SELECT src, dst, type, detail FROM edges"):
            self.out.setdefault(s, []).append((d, t, det))
            self.inn.setdefault(d, []).append((s, t, det))
        self.kinds = list(self.db.execute("SELECT kind, grp, resource, namespaced, short FROM kinds"))
        self.now = self.meta.get("taken_at") or time.time()
        self._raw = {}
        self.manifests = None      # set by open_graph from --repo / the git checkout

    def raw(self, i):
        if not i:
            return {}
        if i not in self._raw:
            r = self.db.execute("SELECT raw FROM nodes WHERE id=?", (i,)).fetchone()
            self._raw[i] = json.loads(r[0]) if r and r[0] else {}
        return self._raw[i]

    def ref_id(self, kind, ns, name):
        """Id of kind/ns/name if the snapshot has it (any group), else ''."""
        for i, n in self.nodes.items():
            if n[1].split(".")[0] == kind and n[3] == (ns or "") and n[4] == name:
                return i
        return ""

    def h(self, i):
        n = self.nodes.get(i)
        return (n[5], n[6], n[7]) if n else ("?", "", "")

    def events(self, ids, warnings_only=True):
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        sql = f"SELECT obj, type, reason, message, count, last FROM events WHERE obj IN ({q})"
        if warnings_only:
            sql += " AND type='Warning'"
        return list(self.db.execute(sql + " ORDER BY last DESC", list(ids)))

    def owners(self, i):
        return [s for s, t, _ in self.inn.get(i, []) if t == "owns"]

    def children(self, i):
        return [d for d, t, _ in self.out.get(i, []) if t == "owns"]

    def root(self, i):
        seen = set()
        while True:
            o = self.owners(i)
            if not o or o[0] in seen:
                return i
            seen.add(i)
            i = o[0]

    def descendants(self, i, depth=6):
        out, frontier = [], [i]
        for _ in range(depth):
            nxt = []
            for f in frontier:
                for c in self.children(f):
                    out.append(c); nxt.append(c)
            frontier = nxt
        return out

    def kind_aliases(self):
        al = {}
        for kind, grp, res, _nsd, short in self.kinds:
            for a in [kind.lower(), res.lower(), res.lower().rstrip("s")] + [s.lower() for s in short.split(",") if s] \
                    + ([f"{res}.{grp}".lower(), f"{kind}.{grp}".lower()] if grp else []):
                al.setdefault(a, set()).add(kind)
        for n in self.nodes.values():
            al.setdefault(n[1].lower(), set()).add(n[1])
        return al

    def resolve(self, spec, ns=None):
        """Object spec -> id. Accepts the printed id, kind/name, kind/ns/name, kind.group/name, a short name
        (deploy/web, svc/web) or a bare name; -n narrows. Raises QueryError listing candidates."""
        if spec in self.nodes:
            return spec
        parts = spec.split("/")
        al = self.kind_aliases()
        kinds, name, nsx = None, spec, ns
        if len(parts) == 3:
            kinds, nsx, name = al.get(parts[0].lower()), parts[1], parts[2]
        elif len(parts) == 2:
            kinds, name = al.get(parts[0].lower()), parts[1]
        if len(parts) > 1 and not kinds:
            raise QueryError(f"unknown kind '{parts[0]}' in {spec}")
        cands = [i for i, n in self.nodes.items() if n[4] == name and (not kinds or n[1] in kinds)
                 and (nsx is None or n[3] == nsx)]
        if not cands:
            cands = [i for i, n in self.nodes.items() if n[4].lower() == name.lower() and (not kinds or n[1] in kinds)
                     and (nsx is None or n[3] == nsx)]
        if len(cands) == 1:
            return cands[0]
        if not cands:
            near = [i for i, n in self.nodes.items() if name.lower() in n[4].lower() and (not kinds or n[1] in kinds)][:8]
            raise QueryError(f"no object {spec}" + (f"; similar: {', '.join(near)}" if near else "")
                             + " (find PATTERN lists names)")
        top = [c for c in cands if self.nodes[c][1] in WORKLOAD_ORDER]
        if len(top) == 1 and not kinds:
            return top[0]
        raise QueryError(f"{spec} is ambiguous: {', '.join(sorted(cands)[:10])}")


WORKLOAD_ORDER = ["Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job", "ReplicaSet", "Pod"]


def tag(h):
    return {"fail": "FAIL", "warn": "WARN", "ok": "ok", "-": "-", "?": "?"}.get(h, h)


def line(g, i, extra=""):
    h, r, s = g.h(i)
    body = f"{r}: {s}" if r and s and not s.startswith(r) else (s or r)
    return f"{tag(h):4} {i}  {body}{extra}"


def header(g):
    m = g.meta
    age = time.time() - (m.get("taken_at") or time.time())
    src = "" if m.get("source") == "live" else f", {m.get('source')}"
    scope = f", namespaces {','.join(m['namespaces'])}" if m.get("namespaces") else ""
    print(f"# cluster {m.get('context')} ({m.get('server') or '?'}), snapshot {ago(age)} old{src}{scope}")


def collapse(g, ids):
    """Group sibling ids that share kind+health+reason: 'Pod x3 CrashLoopBackOff (e.g. ...)'."""
    groups = {}
    for i in ids:
        h, r, _ = g.h(i)
        groups.setdefault((g.nodes[i][1], h, r), []).append(i)
    out = []
    for (kind, _h, _r), members in groups.items():
        if len(members) <= 2:
            out += [(m, None) for m in members]
        else:
            out.append((members[0], f"  (+{len(members) - 1} more {kind} with the same status)"))
    return out



# --------------------------------------------------------------------------- repository manifests

MANIFEST_CAP = 3000          # YAML files scanned per repository
_YAML_TOP = re.compile(r"^kind:\s*['\"]?([A-Za-z][\w]*)")
_FLOW_KEY = r"\b{}:\s*['\"]?([^,'\"{{}}\s]+)"


def repo_root(explicit):
    """--repo, else the git checkout the query runs in; None outside a checkout (never scan $HOME)."""
    if explicit:
        return os.path.abspath(explicit)
    try:
        p = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None


def scan_manifests(root):
    """(kind, name) -> [(path, line, namespace or None)] for plain YAML manifests in the repository.
    Line-based (block and flow `metadata`), so it needs no YAML library; templated names are skipped."""
    try:
        p = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "--", "*.yaml", "*.yml"],
                           cwd=root, capture_output=True, text=True, timeout=30)
        files = p.stdout.splitlines() if p.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired):
        files = []
    if not files:   # not a git checkout (an explicit --repo): walk it, skipping hidden and vendored trees
        for d, dirs, names in os.walk(root):
            dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x not in ("node_modules", "vendor"))
            files += [os.path.relpath(os.path.join(d, n), root) for n in sorted(names) if n.endswith((".yaml", ".yml"))]
            if len(files) >= MANIFEST_CAP:
                break
    idx = {}
    for rel in files[:MANIFEST_CAP]:
        path = os.path.join(root, rel)
        try:
            if os.path.getsize(path) > 1_000_000:
                continue
            with open(path, errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        kind = name = ns = None
        kline, in_md, md_indent = 0, False, None
        for n, l in enumerate(lines, 1):
            if l.startswith("---"):
                _index_doc(idx, rel, kind, name, ns, kline)
                kind = name = ns = None
                in_md = False
                continue
            m = _YAML_TOP.match(l)
            if m and kind is None:
                kind, kline = m.group(1), n
                continue
            if l.startswith("metadata:"):
                rest = l[len("metadata:"):].strip()
                if rest.startswith("{"):
                    mn = re.search(_FLOW_KEY.format("name"), rest)
                    mns = re.search(_FLOW_KEY.format("namespace"), rest)
                    name = name or (mn.group(1) if mn else None)
                    ns = ns or (mns.group(1) if mns else None)
                else:
                    in_md, md_indent = True, None
                continue
            if in_md:
                if l and not l[0].isspace():
                    in_md = False
                    continue
                if not l.strip() or l.strip().startswith("#"):
                    continue
                indent = len(l) - len(l.lstrip())
                md_indent = md_indent or indent
                if indent == md_indent:
                    m = re.match(r"\s+(name|namespace):\s*['\"]?([^'\"\s#]+)", l)
                    if m and m.group(1) == "name":
                        name = name or m.group(2)
                    elif m:
                        ns = ns or m.group(2)
        _index_doc(idx, rel, kind, name, ns, kline)
    return idx


def _index_doc(idx, rel, kind, name, ns, kline):
    if kind and name and "{{" not in name:
        idx.setdefault((kind, name), []).append((rel, kline, ns))


def manifest_of(g, i):
    """Where the repository defines object i (pods and ReplicaSets map to their top owner): 'path:line', or
    None when nothing in the repository declares it. '' when there is no repository to look in."""
    if g.manifests is None:
        return ""
    r = g.root(i) if g.nodes[i][1] in ("Pod", "ReplicaSet", "Job") else i
    kind, ns, name = g.nodes[r][1], g.nodes[r][3], g.nodes[r][4]
    locs = [f"{p}:{n}" for p, n, mns in g.manifests.get((kind.split(".")[0], name), []) if not mns or not ns or mns == ns]
    return ", ".join(locs[:2]) if locs else None


def where(g, i):
    m = manifest_of(g, i)
    if m == "":
        return ""
    return f"defined at {m}" if m else "not defined in this repository"


def candidates(g, i, n=3):
    """Existing objects a missing reference probably meant: same kind, closest names, same namespace first."""
    kind, ns, name = g.nodes[i][1], g.nodes[i][3], g.nodes[i][4]
    pool = [(j, x) for j, x in g.nodes.items() if x[1] == kind and x[6] not in ("missing",) and j != i
            and x[4] != "kube-root-ca.crt" and (x[3] not in SYSTEM_NS or ns in SYSTEM_NS)]
    same = [j for j, x in pool if x[4] == name]
    ranked = sorted(pool, key=lambda jx: (jx[1][3] != ns, -difflib.SequenceMatcher(None, jx[1][4], name).ratio()))
    out = []
    for j in same:
        out.append(f"{j} (same name, other namespace)")
    for j, _x in ranked:
        if len(out) >= n + len(same):
            break
        if j in same:
            continue
        note = ""
        if kind == "StorageClass" and ((g.raw(j).get("metadata") or {}).get("annotations") or {}).get(
                "storageclass.kubernetes.io/is-default-class") == "true":
            note = " (default)"
        out.append(j + note)
    return out


def webhook_lines(g, i):
    """Per webhook: what it intercepts, how it fails, and which namespaces it matches in this snapshot."""
    o = g.raw(i)
    namespaces = [(j, g.raw(j)) for j, x in g.nodes.items() if x[1] == "Namespace"]
    rows = []
    for w in o.get("webhooks") or []:
        rules = "; ".join(f"{'/'.join(r.get('operations') or ['*'])} {','.join(r.get('resources') or ['*'])}"
                          for r in w.get("rules") or [])
        nsel = w.get("namespaceSelector")
        if nsel and (nsel.get("matchLabels") or nsel.get("matchExpressions")):
            hit = [x["metadata"]["name"] for _j, x in namespaces if selects(nsel, x)]
            scope = f"{len(hit)} namespaces now ({', '.join(hit[:6])}{' ...' if len(hit) > 6 else ''})" if hit else \
                "no namespace now (selector " + json.dumps(nsel, sort_keys=True) + ")"
        else:
            scope = "every namespace"
        osel = " objectSelector " + json.dumps(w["objectSelector"], sort_keys=True) if (w.get("objectSelector") or {}).get("matchLabels") \
            or (w.get("objectSelector") or {}).get("matchExpressions") else ""
        svc = (w.get("clientConfig") or {}).get("service") or {}
        target = f"Service {svc.get('namespace')}/{svc.get('name')}" if svc else (w.get("clientConfig") or {}).get("url", "?")
        rows.append(f"webhook {w.get('name')}: {rules or 'no rules'}; failurePolicy {w.get('failurePolicy', 'Fail')}; "
                    f"matches {scope}{osel}; -> {target}")
    return rows

# --------------------------------------------------------------------------- queries

def q_health(g, a):
    header(g)
    nodes = [i for i, n in g.nodes.items() if n[1] == "Node"]
    alloc_c = alloc_m = req_c = req_m = use_c = use_m = 0.0
    has_use = False
    for i in nodes:
        o = g.raw(i)
        al = (o.get("status") or {}).get("allocatable") or {}
        alloc_c += cpu(al.get("cpu")); alloc_m += mem(al.get("memory"))
        r = o.get("_requests") or [0, 0]
        req_c += r[0]; req_m += r[1]
        if o.get("_usage"):
            has_use = True; use_c += o["_usage"][0]; use_m += o["_usage"][1]
    if nodes:
        def pct(x, y):
            return f"{100 * x / y:.0f}%" if y else "?"
        use = f", used {fcpu(use_c)} / {fmem(use_m)}" if has_use else ", usage n/a (no metrics API)"
        print(f"nodes {len(nodes)}: allocatable cpu {fcpu(alloc_c)} mem {fmem(alloc_m)}; requested cpu {fcpu(req_c)} "
              f"({pct(req_c, alloc_c)}) mem {fmem(req_m)} ({pct(req_m, alloc_m)}){use}")
    bad = [i for i, n in g.nodes.items() if n[5] in ("fail", "warn") and (not a.namespace or n[3] in a.namespace)]
    if not bad:
        print("no unhealthy objects" + (f" in {', '.join(a.namespace)}" if a.namespace else ""))
        errs = g.meta.get("errors") or []
        if errs:
            print(f"not collected ({len(errs)}): {'; '.join(errs[:3])}")
        return
    groups = {}
    for i in bad:
        groups.setdefault(g.root(i), set()).add(i)
    nf = sum(1 for i in bad if g.nodes[i][5] == "fail")
    print(f"{nf} fail, {len(bad) - nf} warn, in {len(groups)} groups (ok objects hidden)")

    def order(r):
        n = g.nodes[r]
        sev = 0 if any(g.nodes[x][5] == "fail" for x in groups[r]) else 1
        scope = 0 if not n[3] else (1 if n[3] in SYSTEM_NS else 2)
        return (scope, n[3], sev, WORKLOAD_ORDER.index(n[1]) if n[1] in WORKLOAD_ORDER else 99, r)

    shown, cur_ns = 0, None
    for r in sorted(groups, key=order):
        if shown >= a.max_rows:
            print(f"... {len(groups) - shown} more groups (--max-rows N, or health -n NS)")
            break
        ns = g.nodes[r][3] or "(cluster-scoped)"
        if ns != cur_ns:
            print(f"\n{ns}")
            cur_ns = ns
        members = groups[r] - {r}
        print("  " + line(g, r))
        leaves = [m for m in members if not any(c in members for c in g.children(m))]
        for m, more in collapse(g, sorted(leaves)):
            print("     └ " + line(g, m) + (more or ""))
        if g.nodes[r][6] == "missing":
            users = sorted({g.root(s) for s, _t, _d in g.inn.get(r, [])})
            print(f"     used by {', '.join(users[:6])}{' ...' if len(users) > 6 else ''}")
        shown += 1
    errs = g.meta.get("errors") or []
    if errs:
        print(f"\nnot collected ({len(errs)}): {'; '.join(errs[:3])}{' ...' if len(errs) > 3 else ''}")
    print("\nnext: why OBJ (cause chain, events, logs) for the top group")


def dep_lines(g, i, cap):
    rows = []
    for d, t, det in sorted(g.out.get(i, [])):
        if t == "owns":
            continue
        rows.append(f"{t} " + line(g, d, f"  [{det}]" if det else ""))
    return rows[:cap], len(rows) - min(len(rows), cap)


def user_lines(g, i, cap):
    rows = []
    for s, t, det in sorted(g.inn.get(i, [])):
        if t == "owns":
            continue
        rows.append(f"{t} by " + line(g, s, f"  [{det}]" if det else ""))
    return rows[:cap], len(rows) - min(len(rows), cap)


def event_lines(g, ids, cap, warnings_only=True):
    evs = g.events(ids, warnings_only)
    rows = []
    for obj, typ, reason, msg, count, last in evs[:cap]:
        rows.append(f"{typ[:4]} {reason} x{count} {ago(g.now - last) if last else '?'} ago  {obj}: {msg[:220]}")
    return rows, len(evs) - len(rows)


DEP_EDGES = ("uses", "selects", "routes-to", "calls", "scales", "bound-to", "runs-on")


def cause(g, i, seen=None):
    """The most specific failure under or behind i. Failing dependencies of i or its descendants are
    followed to the end of their chain (pod -> PVC -> missing StorageClass); a missing object wins, then
    the deepest failing dependency, then the deepest failing pod, then i's own reason."""
    seen = seen or {i}
    scope = [i] + g.descendants(i)
    found = []
    for x in scope:
        for d, t, det in g.out.get(x, []):
            if t in DEP_EDGES and g.h(d)[0] == "fail" and d not in seen:
                seen.add(d)
                deeper = cause(g, d, seen) if g.nodes[d][6] not in ("missing", "missing key") else (d, None, None)
                c = deeper[0] or d
                found.append((g.h(c)[1].startswith("missing"), c, deeper[1] or x, deeper[2] or det))
    if found:
        found.sort(key=lambda f: not f[0])
        return found[0][1], found[0][2], found[0][3]
    for x in reversed(scope):
        if g.h(x)[0] == "fail" and g.nodes[x][1] == "Pod":
            return x, None, None
    for x in scope:
        if g.h(x)[0] == "fail":
            return x, None, None
    return None, None, None


def pod_logs(g, pod, tail):
    """Last lines of the failing container (previous instance when it restarted). Live snapshots only."""
    if g.meta.get("source") != "live":
        return []
    o = g.raw(pod)
    st = o.get("status") or {}
    for cs in (st.get("initContainerStatuses") or []) + (st.get("containerStatuses") or []):
        state = cs.get("state") or {}
        broken = (state.get("waiting") or {}).get("reason") in ("CrashLoopBackOff", "RunContainerError") \
            or (state.get("terminated") or {}).get("exitCode", 0) != 0 or not cs.get("ready")
        if not broken or (state.get("waiting") or {}).get("reason") in ("ImagePullBackOff", "ErrImagePull",
                                                                          "CreateContainerConfigError", "InvalidImageName"):
            continue
        prev = cs.get("restartCount", 0) > 0 and "running" not in state
        args = ["logs", g.nodes[pod][4], "-n", g.nodes[pod][3], "-c", cs.get("name"), f"--tail={tail}"] + (["--previous"] if prev else [])
        rc, out, err = kubectl(g.meta.get("context"), args, timeout=30)
        text = out.rstrip().splitlines() if rc == 0 else clean_err(err)
        if not text:
            text = ["(the container wrote nothing to stdout/stderr)"]
        return [f"logs {pod} -c {cs.get('name')}{' --previous' if prev else ''} (last {len(text)} lines):"] + [f"  | {t[:240]}" for t in text]
    return []


def q_why(g, a):
    header(g)
    for spec in a.objects:
        try:
            i = g.resolve(spec, a.namespace[0] if a.namespace else None)
        except QueryError as e:
            print(f"! {e}")
            continue
        print("\n" + line(g, i))
        loc = where(g, i)
        if loc and g.nodes[i][6] not in ("missing",):
            print(loc)
        if g.nodes[i][1] in ("ValidatingWebhookConfiguration", "MutatingWebhookConfiguration"):
            for r in webhook_lines(g, i):
                print(r)
        chain = []
        x = i
        while g.owners(x):
            x = g.owners(x)[0]
            chain.append(x)
        if chain:
            print("owned by " + " <- ".join(f"{c} ({tag(g.h(c)[0])})" for c in chain))
        kids = g.descendants(i, 3)
        if kids:
            print("children:")
            for k, more in collapse(g, [k for k in kids if g.h(k)[0] != "-" or g.nodes[k][1] == "Pod"])[:a.cap]:
                print("  " + line(g, k) + (more or ""))
        deps, more = dep_lines(g, i, a.cap)
        for k in kids:
            if g.nodes[k][1] == "Pod":
                extra, _ = dep_lines(g, k, a.cap)
                deps += [d for d in extra if d.split("  ")[0] not in " ".join(deps) and ("FAIL" in d or "WARN" in d)]
                break
        if deps:
            print("depends on:")
            for d in deps[:a.cap]:
                print("  " + d)
            if more:
                print(f"  ... {more} more (--all)")
        users, more = user_lines(g, i, a.cap)
        for k in kids:
            if g.nodes[k][1] == "Pod":
                for s, t, _ in g.inn.get(k, []):
                    row = "selects its pods: " + line(g, s)
                    if t == "selects" and row not in users:
                        users.append(row)
        if users:
            print("used by:")
            for u in users:
                print("  " + u)
            if more:
                print(f"  ... {more} more (--all)")
        if g.nodes[i][1] == "Service" and g.nodes[i][6] == "NoPods":
            sel = (g.raw(i).get("spec") or {}).get("selector") or {}
            want = json.dumps(sel, sort_keys=True)
            seen = {}
            for p, n in g.nodes.items():
                if n[1] == "Pod" and n[3] == g.nodes[i][3]:
                    labels = (g.raw(p).get("metadata") or {}).get("labels") or {}
                    shared = {k: labels[k] for k in sel if k in labels}
                    if shared:
                        key = json.dumps(shared, sort_keys=True)
                        seen.setdefault(key, g.root(p))
            ranked = sorted(seen, key=lambda k: -difflib.SequenceMatcher(None, k, want).ratio())
            print(f"closest pod labels for selector {want}:" if ranked else "no pod in the namespace shares a selector key")
            for k in ranked[:3]:
                print(f"  {seen[k]} pods have {k}")
        ev_ids = [i] + kids + [d for d, t, _ in g.out.get(i, []) if t in ("uses", "bound-to")]
        evs, more = event_lines(g, ev_ids, a.events)
        if evs:
            print("warning events:")
            for e in evs:
                print("  " + e)
            if more:
                print(f"  ... {more} more (events {i})")
        c, via, det = cause(g, i)
        if c:
            reason = g.h(c)[1]
            key = "missing" if reason.startswith("missing") else reason
            print(f"cause: {c} {reason}: {g.h(c)[2]}" + (f" (via {via}{', ' + det if det else ''})" if via else ""))
            if reason == "missing":
                near = candidates(g, c)
                scope = "the cluster" if not g.meta.get("namespaces") else "the snapshot's namespaces"
                print(f"searched {scope}: no {g.nodes[c][1]} named {g.nodes[c][4]} in any namespace; existing: "
                      + (", ".join(near) if near else "none"))
            elif reason == "missing key":
                print(f"keys present: {', '.join(g.raw(c).get('dataKeys') or []) or 'none'}")
            locs = [(x, where(g, x)) for x in dict.fromkeys(
                (g.root(x) if g.nodes[x][1] in ("Pod", "ReplicaSet", "Job") else x) for x in [c] + ([via] if via else []))]
            locs = [f"{x} {w}" for x, w in locs if w]
            if locs:
                print("manifests: " + "; ".join(locs))
            adv = ADVICE.get(key) or ADVICE.get(key.split("=")[0])
            if not adv and g.nodes[c][1] not in BUILTIN_KINDS:
                adv = ("reported by the controller that reconciles this custom resource: its message is the "
                       "controller's diagnosis; the controller's own logs have the detail")
            if adv:
                print(f"advice: {adv}")
            if g.nodes[c][1] == "Pod" and not a.no_logs:
                for l in pod_logs(g, c, a.tail):
                    print(l)
            nxt = next_steps(g, c)
            if nxt:
                print("next: " + " ; ".join(nxt))
        elif g.h(i)[0] in ("ok", "-"):
            print("cause: none found; the object and everything it depends on look healthy in this snapshot")


def next_steps(g, i):
    kind, ns, name = g.nodes[i][1], g.nodes[i][3], g.nodes[i][4]
    ctx = g.meta.get("context")
    k = f"kubectl --context {ctx}" + (f" -n {ns}" if ns else "")
    r = g.h(i)[1]
    if kind == "Pod":
        if r in ("CrashLoopBackOff", "Error", "OOMKilled", "ProbeFailing", "NotReady", "Restarting"):
            restarted = any(c.get("restartCount", 0) for c in (g.raw(i).get("status") or {}).get("containerStatuses") or [])
            return [f"{k} logs {name}{' --previous' if restarted else ''} --tail=100", f"{k} describe pod {name}"]
        return [f"{k} describe pod {name}"]
    if r.startswith("missing"):
        return [f"query used-by {i} (what needs it)"]
    return [f"{k} describe {kind.lower()} {name}"]


def q_show(g, a):
    header(g)
    for spec in a.objects:
        try:
            i = g.resolve(spec, a.namespace[0] if a.namespace else None)
        except QueryError as e:
            print(f"! {e}")
            continue
        o = g.raw(i)
        md = o.get("metadata") or {}
        print("\n" + line(g, i))
        loc = where(g, i)
        if loc:
            print(loc)
        if g.nodes[i][1] in ("ValidatingWebhookConfiguration", "MutatingWebhookConfiguration"):
            for r in webhook_lines(g, i):
                print(r)
        if md.get("creationTimestamp"):
            print(f"age {ago(g.now - ts(md['creationTimestamp']))}" + (f", labels {json.dumps(md.get('labels'), sort_keys=True)[:200]}" if md.get("labels") else ""))
        pod_spec = podspec_of(g.nodes[i][1], o)
        if pod_spec:
            for c in (pod_spec.get("initContainers") or []) + (pod_spec.get("containers") or []):
                res = c.get("resources") or {}
                print(f"container {c.get('name')}: {c.get('image')}  requests {json.dumps(res.get('requests') or {})} limits {json.dumps(res.get('limits') or {})}")
            want = (o.get("spec") or {}).get("replicas")
            if want is not None:
                pc, pm = pod_requests(pod_spec)
                print(f"requests per pod cpu {fcpu(pc)} mem {fmem(pm)}; x{want} replicas = cpu {fcpu(pc * want)} mem {fmem(pm * want)}")
        if o.get("_usage"):
            print(f"usage now: cpu {fcpu(o['_usage'][0])} mem {fmem(o['_usage'][1])}")
        if o.get("_requests"):
            print(f"requested by pods: cpu {fcpu(o['_requests'][0])} mem {fmem(o['_requests'][1])}")
        own = g.owners(i)
        if own:
            print("owner: " + ", ".join(own))
        kids = g.children(i)
        if kids:
            print("children:")
            for k, more in collapse(g, kids)[:a.cap]:
                print("  " + line(g, k) + (more or ""))
        for title, (rows, more) in (("uses:", dep_lines(g, i, a.cap)), ("used by:", user_lines(g, i, a.cap))):
            if rows:
                print(title)
                for r in rows:
                    print("  " + r)
                if more:
                    print(f"  ... {more} more (--all)")
        evs, more = event_lines(g, [i], a.events)
        if evs:
            print("warning events:")
            for e in evs:
                print("  " + e)


def podspec_of(kind, o):
    spec = o.get("spec") or {}
    if kind == "Pod":
        return spec
    if kind == "CronJob":
        return (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec")
    if "template" in spec:
        return (spec.get("template") or {}).get("spec")
    return None


def q_used_by(g, a):
    header(g)
    for spec in a.objects:
        try:
            i = g.resolve(spec, a.namespace[0] if a.namespace else None)
        except QueryError as e:
            print(f"! {e}")
            continue
        print("\n" + line(g, i))
        seen, frontier = {i}, [i]
        for depth in range(1, a.depth + 1):
            nxt, rows = [], []
            for f in frontier:
                srcs = [(s, f"{t} " + (f"[{det}]" if det else "")) for s, t, det in g.inn.get(f, []) if t != "owns"]
                srcs += [(s, "owns") for s in g.owners(f)]
                # a Service/PDB that selects f's pods depends on f too
                for c in g.descendants(f, 2):
                    for s, t, _ in g.inn.get(c, []):
                        if t == "selects":
                            srcs.append((s, f"selects pods of {f}"))
                for s, why in srcs:
                    s = s if g.nodes[s][1] not in ("ReplicaSet", "Pod", "Job") else g.root(s)
                    if s in seen:
                        continue
                    seen.add(s)
                    nxt.append(s)
                    rows.append(f"  {why.strip():28} " + line(g, s))
            if not rows:
                break
            print(f"depth {depth}:")
            for r in rows[:a.max_rows]:
                print(r)
            if len(rows) > a.max_rows:
                print(f"  ... {len(rows) - a.max_rows} more (--max-rows N)")
            frontier = nxt
        if len(seen) == 1:
            print("nothing in the snapshot depends on it")


def q_tree(g, a):
    header(g)
    if a.objects:
        roots = []
        for spec in a.objects:
            try:
                roots.append(g.resolve(spec, a.namespace[0] if a.namespace else None))
            except QueryError as e:
                print(f"! {e}")
    else:
        if not a.namespace:
            raise QueryError("tree needs an object or -n NAMESPACE")
        roots = sorted(i for i, n in g.nodes.items() if n[3] in a.namespace and not g.owners(i)
                       and n[1] not in ("ConfigMap", "Secret", "ServiceAccount", "Event", "RoleBinding", "Role")
                       and n[6] != "missing")

    def walk(i, depth):
        for k, more in collapse(g, g.children(i)):
            if g.nodes[k][6] == "OldRevision" and not a.all:
                continue
            print("  " * depth + "└ " + line(g, k) + (more or ""))
            walk(k, depth + 1)
    for r in roots[:a.max_rows]:
        print(line(g, r))
        walk(r, 1)
    if len(roots) > a.max_rows:
        print(f"... {len(roots) - a.max_rows} more roots (--max-rows N)")


def q_find(g, a):
    header(g)
    pat = a.pattern
    kinds = g.kind_aliases().get(a.kind.lower()) if a.kind else None
    if a.kind and not kinds:
        raise QueryError(f"unknown kind {a.kind}")
    rows = []
    for i, n in sorted(g.nodes.items()):
        if kinds and n[1] not in kinds or a.namespace and n[3] not in a.namespace:
            continue
        if a.unhealthy and n[5] not in ("fail", "warn"):
            continue
        if pat and not (fnmatch.fnmatch(n[4], pat) if any(ch in pat for ch in "*?[") else pat.lower() in n[4].lower()):
            continue
        rows.append(i)
    for i in rows[:a.max_rows]:
        print(line(g, i))
    if len(rows) > a.max_rows:
        print(f"... {len(rows) - a.max_rows} more (--max-rows N, --kind, -n)")
    if not rows:
        print("no match")


def q_events(g, a):
    header(g)
    if a.objects:
        ids = []
        for spec in a.objects:
            i = g.resolve(spec, a.namespace[0] if a.namespace else None)
            ids += [i] + g.descendants(i)
    else:
        ids = [i for i, n in g.nodes.items() if not a.namespace or n[3] in a.namespace]
        ids += [e[0] for e in g.db.execute("SELECT DISTINCT obj FROM events") if e[0] not in g.nodes]
    rows, more = event_lines(g, ids, a.max_rows, warnings_only=not a.all)
    for r in rows:
        print(r)
    if more:
        print(f"... {more} more (--max-rows N)")
    if not rows:
        print("no warning events" + (" (--all includes Normal events)" if not a.all else ""))


def q_crds(g, a):
    header(g)
    crds = sorted(i for i, n in g.nodes.items() if n[1] == "CustomResourceDefinition")
    inst = {}
    for c in crds:
        for s, t, _ in g.inn.get(c, []):
            if t == "instance-of":
                inst.setdefault(c, []).append(s)
    for c in crds:
        o = g.raw(c)
        spec = o.get("spec") or {}
        members = inst.get(c, [])
        bad = [m for m in members if g.h(m)[0] in ("fail", "warn")]
        print(f"{tag(g.h(c)[0]):4} {(spec.get('names') or {}).get('kind')} ({spec.get('group')}, {spec.get('scope')}): "
              f"{len(members)} instances" + (f", {len(bad)} unhealthy: {', '.join(bad[:4])}" if bad else ""))
    if not crds:
        print("no CustomResourceDefinitions")


def q_overview(g, a):
    header(g)
    per = {}
    for i, n in g.nodes.items():
        if not n[3] or n[6] == "missing":
            continue
        p = per.setdefault(n[3], {"pods": 0, "running": 0, "work": 0, "bad": 0, "rc": 0.0, "rm": 0.0, "uc": 0.0, "um": 0.0, "use": False})
        if n[5] in ("fail", "warn"):
            p["bad"] += 1
        if n[1] in ("Deployment", "StatefulSet", "DaemonSet", "CronJob"):
            p["work"] += 1
        if n[1] == "Pod":
            o = g.raw(i)
            phase = (o.get("status") or {}).get("phase")
            p["pods"] += 1
            if phase in ("Succeeded", "Failed"):
                continue
            p["running"] += phase == "Running"
            c, m = pod_requests(o.get("spec") or {})
            p["rc"] += c; p["rm"] += m
            if o.get("_usage"):
                p["use"] = True; p["uc"] += o["_usage"][0]; p["um"] += o["_usage"][1]
    print(f"{'namespace':28} {'pods':>9} {'workloads':>9} {'req cpu':>8} {'used':>6} {'req mem':>8} {'used':>7} unhealthy")
    for ns, p in sorted(per.items(), key=lambda kv: -kv[1]["rc"])[:a.max_rows]:
        uc = fcpu(p["uc"]) if p["use"] else "-"
        um = fmem(p["um"]) if p["use"] else "-"
        print(f"{ns[:28]:28} {p['running']:>4}/{p['pods']:<4} {p['work']:>9} {fcpu(p['rc']):>8} {uc:>6} {fmem(p['rm']):>8} {um:>7} {p['bad'] or ''}")
    if len(per) > a.max_rows:
        print(f"... {len(per) - a.max_rows} more namespaces (--max-rows N)")
    kinds = {}
    for n in g.nodes.values():
        kinds[n[1]] = kinds.get(n[1], 0) + 1
    print("kinds: " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])[:20]))



# --------------------------------------------------------------------------- cost waste

PRICING = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "gke-cost-discovery", "reference", "pricing.json")
DEFAULT_PRICE = {"default": {"vcpu_hour": 0.0218, "gb_hour": 0.0029}, "hours_per_month": 730,
                 "pd_gb_month": {"standard": 0.04, "balanced": 0.10, "ssd": 0.17}, "lb_month": 18.25}
ENV_NS = "*preview*,*review*,*test*,pr-*,*-pr-*,*ephemeral*,*sandbox*"
JANITOR = ("janitor/ttl", "janitor/expires")


def load_price():
    try:
        with open(PRICING) as f:
            return {**DEFAULT_PRICE, **json.load(f)}
    except (OSError, ValueError):
        return DEFAULT_PRICE


def month(price, c, m):
    p = price["default"]
    return (c * p["vcpu_hour"] + m / 1024 ** 3 * p["gb_hour"]) * price["hours_per_month"]


def disk_month(price, size, sc_obj):
    """$ per month of a disk: GB x the PD type its StorageClass provisions (standard when unknown)."""
    params = (sc_obj or {}).get("parameters") or {}
    t = str(params.get("type", "pd-standard")).replace("pd-", "")
    return mem(size) / 1e9 * price["pd_gb_month"].get(t, price["pd_gb_month"]["standard"])


def q_waste(g, a):
    """Live cost waste the snapshot can see; p95-based right-sizing stays with gke-cost-discovery."""
    header(g)
    price = load_price()
    in_ns = lambda n: not a.namespace or n[3] in a.namespace   # noqa: E731
    total = 0.0
    sections = []

    # 1. requests reserved by scheduled pods that do no work (crashing, not starting, never Ready)
    held = {}
    for i, n in g.nodes.items():
        if n[1] != "Pod" or n[5] != "fail" or not in_ns(n):
            continue
        o = g.raw(i)
        if not (o.get("spec") or {}).get("nodeName") or (o.get("status") or {}).get("phase") in ("Succeeded", "Failed"):
            continue
        c, m = pod_requests(o.get("spec") or {})
        e = held.setdefault(g.root(i), [0.0, 0.0, 0, n[6]])
        e[0] += c; e[1] += m; e[2] += 1
    rows = sorted(held.items(), key=lambda kv: -month(price, kv[1][0], kv[1][1]))
    cost = sum(month(price, v[0], v[1]) for _k, v in rows)
    sections.append((f"requests held by pods that do no work: ~${cost:.0f}/month", [
        f"{k}  {v[2]} pod(s) {v[3]}, cpu {fcpu(v[0])} mem {fmem(v[1])}" for k, v in rows]))
    total += cost

    # 2. requested far above usage right now (one sample, so a lead, not a verdict)
    over = {}
    for i, n in g.nodes.items():
        if n[1] != "Pod" or n[5] != "ok" or not in_ns(n) or n[3] in SYSTEM_NS:
            continue
        o = g.raw(i)
        if not o.get("_usage"):
            continue
        c, m = pod_requests(o.get("spec") or {})
        uc, um = o["_usage"]
        spare_c, spare_m = max(0.0, c - 2 * uc), max(0.0, m - 2 * um)   # keep 2x current as headroom
        if (spare_c >= 0.25 and uc < 0.25 * c) or (spare_m >= 256 * 1024 ** 2 and um < 0.25 * m):
            e = over.setdefault(g.root(i), [0.0, 0.0, 0.0, 0.0, 0])
            e[0] += c; e[1] += m; e[2] += uc; e[3] += um; e[4] += 1
    rows = sorted(over.items(), key=lambda kv: -month(price, kv[1][0] - 2 * kv[1][2], kv[1][1] - 2 * kv[1][3]))
    cost = sum(month(price, max(0, v[0] - 2 * v[2]), max(0, v[1] - 2 * v[3])) for _k, v in rows)
    sections.append((f"requested far above current usage (one sample; size from gke-cost-discovery p95): ~${cost:.0f}/month", [
        f"{k}  {v[4]} pod(s) request cpu {fcpu(v[0])} mem {fmem(v[1])}, use cpu {fcpu(v[2])} mem {fmem(v[3])}" for k, v in rows]))
    total += cost

    # 3. disks that nothing mounts
    rows, cost = [], 0.0
    for i, n in g.nodes.items():
        if not in_ns(n) and n[1] != "PersistentVolume":
            continue
        o = g.raw(i)
        if n[1] == "PersistentVolumeClaim" and (o.get("status") or {}).get("phase") == "Bound":
            if any(g.nodes.get(s, [0, ""])[1] == "Pod" for s, t, _ in g.inn.get(i, []) if t == "uses"):
                continue
            size = ((o.get("status") or {}).get("capacity") or {}).get("storage") or \
                (((o.get("spec") or {}).get("resources") or {}).get("requests") or {}).get("storage", "0")
            sc = g.raw(g.ref_id("StorageClass", "", (o.get("spec") or {}).get("storageClassName") or ""))
            c = disk_month(price, size, sc)
            rows.append(f"{i}  Bound {size}, no pod mounts it  ~${c:.2f}/month")
            cost += c
        elif n[1] == "PersistentVolume" and (o.get("status") or {}).get("phase") in ("Released", "Available"):
            if a.namespace and ((o.get("spec") or {}).get("claimRef") or {}).get("namespace") not in a.namespace:
                continue
            size = ((o.get("spec") or {}).get("capacity") or {}).get("storage", "0")
            sc = g.raw(g.ref_id("StorageClass", "", (o.get("spec") or {}).get("storageClassName") or ""))
            c = disk_month(price, size, sc)
            rows.append(f"{i}  {(o.get('status') or {}).get('phase')} {size}, reclaim {(o.get('spec') or {}).get('persistentVolumeReclaimPolicy')}  ~${c:.2f}/month")
            cost += c
    sections.append((f"disks nothing mounts: ~${cost:.2f}/month", rows))
    total += cost

    # 4. load balancers (one forwarding rule each), worst first
    rows, cost = [], 0.0
    for i, n in sorted(g.nodes.items(), key=lambda kv: kv[1][5] != "fail"):
        if n[1] == "Service" and in_ns(n) and (g.raw(i).get("spec") or {}).get("type") == "LoadBalancer":
            cost += price["lb_month"]
            rows.append(f"{i}  {tag(n[5])} {n[7]}  ~${price['lb_month']:.0f}/month")
    sections.append((f"load balancers: ~${cost:.0f}/month", rows))
    total += cost

    # 5. lifecycle: finished Jobs kept forever, test environments without a janitor TTL
    rows = []
    min_age = a.min_age * 3600
    for i, n in g.nodes.items():
        if n[1] != "Job" or not in_ns(n) or g.owners(i):
            continue
        o = g.raw(i)
        if n[6] in ("Complete", "BackoffLimitExceeded", "DeadlineExceeded", "Failed") and \
                (o.get("spec") or {}).get("ttlSecondsAfterFinished") is None:
            rows.append(f"{i}  {n[6]}, no ttlSecondsAfterFinished: kept (with its pods) until deleted")
    patterns = [p.strip() for p in a.env_ns.split(",") if p.strip()]
    ns_ann = {x[4]: ((g.raw(j).get("metadata") or {}).get("annotations") or {}) for j, x in g.nodes.items() if x[1] == "Namespace"}
    for i, n in g.nodes.items():
        if n[1] not in ("Deployment", "StatefulSet") or not in_ns(n) or g.owners(i):
            continue
        if not any(fnmatch.fnmatch(n[3], p) for p in patterns):
            continue
        o = g.raw(i)
        ann = (o.get("metadata") or {}).get("annotations") or {}
        if any(k in ann for k in JANITOR) or any(k in ns_ann.get(n[3], {}) for k in JANITOR):
            continue
        age = g.now - (n[8] or g.now)
        want = (o.get("spec") or {}).get("replicas", 1)
        if want and age >= min_age:
            c, m = pod_requests(podspec_of(n[1], o) or {})
            rows.append(f"{i}  up {ago(age)}, {want} replicas, cpu {fcpu(c * want)} mem {fmem(m * want)} "
                        f"(~${month(price, c * want, m * want):.0f}/month), no janitor/ttl or janitor/expires")
    sections.append((f"lifecycle (test namespaces matching {a.env_ns}; older than {a.min_age:g}h)", rows))

    # 6. what keeps nodes from scaling down (the unallocated bucket)
    rows = []
    for i, n in g.nodes.items():
        if n[1] != "Pod" or not in_ns(n) or n[3] in SYSTEM_NS:
            continue
        o = g.raw(i)
        if (o.get("status") or {}).get("phase") in ("Succeeded", "Failed"):
            continue
        md = o.get("metadata") or {}
        why = []
        if ((md.get("annotations") or {}).get("cluster-autoscaler.kubernetes.io/safe-to-evict")) == "false":
            why.append("safe-to-evict=false")
        if not md.get("ownerReferences"):
            why.append("no controller (bare pod)")
        if any("emptyDir" in v or "hostPath" in v for v in (o.get("spec") or {}).get("volumes") or []) and \
                ((md.get("annotations") or {}).get("cluster-autoscaler.kubernetes.io/safe-to-evict")) != "true":
            why.append("local storage (emptyDir/hostPath)")
        if why:
            rows.append(f"{i}  {', '.join(why)}")
    for i, n in g.nodes.items():
        if n[1] == "PodDisruptionBudget" and in_ns(n):
            st = g.raw(i).get("status") or {}
            if st.get("disruptionsAllowed", 1) == 0 and st.get("expectedPods", 0) > 0:
                rows.append(f"{i}  allows 0 disruptions ({st.get('currentHealthy', 0)}/{st.get('expectedPods', 0)} healthy): blocks draining its nodes")
    sections.append(("scale-down blockers", rows))
    rows = []
    for i, n in g.nodes.items():
        if n[1] != "Node":
            continue
        o = g.raw(i)
        al = (o.get("status") or {}).get("allocatable") or {}
        r = o.get("_requests") or [0, 0]
        fc, fm = r[0] / max(cpu(al.get("cpu")), 1e-9), r[1] / max(mem(al.get("memory")), 1)
        if fc < 0.4 and fm < 0.4:
            free_c, free_m = cpu(al.get("cpu")) - r[0], mem(al.get("memory")) - r[1]
            rows.append(f"{i}  requested cpu {fc:.0%} mem {fm:.0%}: ~${month(price, free_c, free_m):.0f}/month unallocated; consolidation candidate")
    sections.append(("underfilled nodes", rows))

    for title, rows in sections:
        print(f"\n{title}" if rows else f"\n{title.split(': ~$')[0]}: none")
        for r in rows[:a.max_rows]:
            print("  " + r)
        if len(rows) > a.max_rows:
            print(f"  ... {len(rows) - a.max_rows} more (--max-rows N)")
    print(f"\nidentified: ~${total:.0f}/month, not counting unallocated node capacity (list prices from "
          "gke-cost-discovery/reference/pricing.json; the billing export is the authority)")
    print("next: gke-cost-discovery for p95-based right-sizing and the node-pool view; k8s-guardrails for TTLs and quotas")


# --------------------------------------------------------------------------- NetworkPolicy reachability

POLICY_CNI = ("calico", "cilium", "anetd", "antrea", "weave", "kube-router", "canal")


def endpoint(g, spec, ns, want_service):
    """An object spec -> (label: id, pod labels, namespace, pod IP, container ports {name: (port, proto)},
    service ports [(port, targetPort, proto)])."""
    i = g.resolve(spec, ns)
    kind = g.nodes[i][1]
    svc_ports = []
    if kind == "Service":
        spec_ = g.raw(i).get("spec") or {}
        svc_ports = [(p.get("port"), p.get("targetPort", p.get("port")), p.get("protocol", "TCP")) for p in spec_.get("ports") or []]
        pods = [d for d, t, _ in g.out.get(i, []) if t == "selects"]
        if not pods:
            labels = spec_.get("selector") or {}
            return i, labels, g.nodes[i][3], None, {}, svc_ports, "the Service selects no pod"
        i_pod = pods[0]
    elif kind == "Pod":
        i_pod = i
    else:
        pods = [k for k in g.descendants(i) if g.nodes[k][1] == "Pod"]
        if not pods:
            o = g.raw(i)
            tmpl = (o.get("spec") or {}).get("template") or {}
            labels = (tmpl.get("metadata") or {}).get("labels") or {}
            ports = {c_p.get("name") or str(c_p.get("containerPort")): (c_p.get("containerPort"), c_p.get("protocol", "TCP"))
                     for c in (tmpl.get("spec") or {}).get("containers") or [] for c_p in c.get("ports") or []}
            return i, labels, g.nodes[i][3], None, ports, svc_ports, "no running pod: evaluated with the template's labels"
        i_pod = pods[0]
    o = g.raw(i_pod)
    labels = (o.get("metadata") or {}).get("labels") or {}
    ports = {c_p.get("name") or str(c_p.get("containerPort")): (c_p.get("containerPort"), c_p.get("protocol", "TCP"))
             for c in (o.get("spec") or {}).get("containers") or [] for c_p in c.get("ports") or []}
    return i, labels, g.nodes[i_pod][3], (o.get("status") or {}).get("podIP"), ports, svc_ports, ""


def policy_types(spec):
    return spec.get("policyTypes") or (["Ingress"] + (["Egress"] if "egress" in spec else []))


def peer_matches(g, peer, pol_ns, labels, ns, ip):
    import ipaddress
    if peer.get("ipBlock"):
        if not ip:
            return False
        try:
            addr = ipaddress.ip_address(ip)
            if addr not in ipaddress.ip_network(peer["ipBlock"]["cidr"], strict=False):
                return False
            return not any(addr in ipaddress.ip_network(x, strict=False) for x in peer["ipBlock"].get("except") or [])
        except ValueError:
            return False
    if "namespaceSelector" in peer:
        ns_obj = g.raw(g.ref_id("Namespace", "", ns)) or {"metadata": {"labels": {"kubernetes.io/metadata.name": ns}}}
        if not selects(peer["namespaceSelector"] or {}, ns_obj):
            return False
    elif ns != pol_ns:
        return False
    return "podSelector" not in peer or selects(peer["podSelector"] or {}, {"metadata": {"labels": labels}})


def port_matches(rule_ports, port, proto, named):
    if not rule_ports:
        return True
    if port is None:
        return None          # unknown destination port: depends on the port used
    for p in rule_ports:
        if p.get("protocol", "TCP") != proto:
            continue
        rp = p.get("port")
        if rp is None:
            return True
        if isinstance(rp, str) and not rp.isdigit():
            rp = (named.get(rp) or (None,))[0]
        if rp is not None and (int(rp) == port or (p.get("endPort") and int(rp) <= port <= p["endPort"])):
            return True
    return False


def rule_text(rule, key):
    peers = []
    for peer in rule.get(key) or []:
        bits = []
        if "namespaceSelector" in peer:
            bits.append("ns " + json.dumps(peer["namespaceSelector"] or {}, sort_keys=True))
        if "podSelector" in peer:
            bits.append("pods " + json.dumps(peer["podSelector"] or {}, sort_keys=True))
        if peer.get("ipBlock"):
            bits.append("ipBlock " + peer["ipBlock"].get("cidr", "?"))
        peers.append(" ".join(bits))
    ports = ",".join(f"{p.get('port', '*')}/{p.get('protocol', 'TCP')}" for p in rule.get("ports") or []) or "any port"
    return f"{'from' if key == 'from' else 'to'} {' | '.join(peers) or 'anywhere'} on {ports}"


def direction(g, side, labels, ns, other, port, proto, named):
    """Is traffic allowed at one end? side 'Egress' (policies selecting the source) or 'Ingress'."""
    key, rules_key = ("to", "egress") if side == "Egress" else ("from", "ingress")
    o_labels, o_ns, o_ip = other
    sel = []
    for j, n in g.nodes.items():
        if n[1] != "NetworkPolicy" or n[3] != ns:
            continue
        spec = g.raw(j).get("spec") or {}
        if side in policy_types(spec) and selects(spec.get("podSelector") or {}, {"metadata": {"labels": labels}}):
            sel.append((j, spec))
    if not sel:
        return True, [f"allowed: no NetworkPolicy selects it for {side}"]
    unknown = False
    for j, spec in sel:
        for k, rule in enumerate(spec.get(rules_key) or [], 1):
            peers_ok = not rule.get(key) or any(peer_matches(g, p, ns, o_labels, o_ns, o_ip) for p in rule[key])
            if not peers_ok:
                continue
            pm = port_matches(rule.get("ports"), port, proto, named)
            if pm:
                return True, [f"allowed by {j} {rules_key} rule {k} ({rule_text(rule, key)})"]
            if pm is None:
                unknown = True
    lines = [f"{'unknown' if unknown else 'DENIED'}: selected by {', '.join(j for j, _ in sel)} for {side}, "
             f"and no rule admits the {'destination' if side == 'Egress' else 'source'}"
             + (" on this port" if port else "")]
    for j, spec in sel:
        rules = spec.get(rules_key) or []
        lines.append(f"  {j}: " + ("; ".join(rule_text(r, key) for r in rules) if rules else f"no {rules_key} rules (deny all)"))
    return (None if unknown else False), lines


HASH_LABELS = ("pod-template-hash", "controller-revision-hash", "pod-template-generation")


def show_labels(labels):
    return json.dumps({k: v for k, v in labels.items() if k not in HASH_LABELS}, sort_keys=True)


def q_reach(g, a):
    header(g)
    ns = a.namespace[0] if a.namespace else None
    src = endpoint(g, a.source, ns, False)
    dst = endpoint(g, a.dest, ns, True)
    s_id, s_labels, s_ns, s_ip, _s_ports, _sp, s_note = src
    d_id, d_labels, d_ns, d_ip, d_ports, d_svc, d_note = dst
    proto = a.protocol
    port = a.port
    if port is None and d_svc:
        sp = d_svc[0]
        tp = sp[1]
        port = int(tp) if isinstance(tp, int) or str(tp).isdigit() else (d_ports.get(tp) or (None,))[0]
        proto = sp[2]
        print(f"port: Service port {sp[0]} -> targetPort {tp}" + (f" = {port}" if port else " (unresolved)"))
    elif port is None and len(d_ports) == 1:
        port, proto = next(iter(d_ports.values()))
    print(f"{s_id} ({s_ns}, {show_labels(s_labels)}) -> {d_id} ({d_ns}, {show_labels(d_labels)}) "
          f"port {port or 'any'}/{proto}")
    for note in (s_note, d_note):
        if note:
            print(f"note: {note}")
    ok_e, lines_e = direction(g, "Egress", s_labels, s_ns, (d_labels, d_ns, d_ip), port, proto, d_ports)
    print("egress from source: " + lines_e[0])
    for l in lines_e[1:]:
        print(l)
    ok_i, lines_i = direction(g, "Ingress", d_labels, d_ns, (s_labels, s_ns, None), port, proto, d_ports)
    print("ingress to destination: " + lines_i[0])
    for l in lines_i[1:]:
        print(l)
    # DNS: an egress policy on the source usually needs an explicit rule for kube-dns
    has_egress = not lines_e[0].startswith("allowed: no NetworkPolicy")
    if has_egress:
        dns = [p for p, n in g.nodes.items() if n[1] == "Pod" and n[3] == "kube-system"
               and ((g.raw(p).get("metadata") or {}).get("labels") or {}).get("k8s-app") == "kube-dns"]
        dlabels = ((g.raw(dns[0]).get("metadata") or {}).get("labels") or {}) if dns else {"k8s-app": "kube-dns"}
        ok_dns, _ = direction(g, "Egress", s_labels, s_ns, (dlabels, "kube-system", None), 53, "UDP", {})
        print("dns from source (kube-dns 53/UDP): " + ("allowed" if ok_dns else
              "DENIED: the source's egress policies have no rule for kube-dns, so name lookups fail before any connection"))
        if not ok_dns:
            sel = {k: v for k, v in dlabels.items() if k == "k8s-app"} or {"k8s-app": "kube-dns"}
            print("  add to one of those egress policies: - to: [{namespaceSelector: {matchLabels: "
                  "{kubernetes.io/metadata.name: kube-system}}, podSelector: {matchLabels: "
                  f"{json.dumps(sel).replace(chr(34), '')}}}}}]\n      ports: [{{port: 53, protocol: UDP}}, {{port: 53, protocol: TCP}}]")
    pols = sorted({w for l in lines_e + lines_i for w in re.findall(r"NetworkPolicy/[\w.-]+/[\w.-]+", l)})
    locs = [f"{p} {where(g, p)}" for p in pols if where(g, p)]
    if locs:
        print("manifests: " + "; ".join(locs))
    verdict = "allowed" if ok_e and ok_i else ("denied" if ok_e is False or ok_i is False else "depends on the port")
    print(f"verdict: {verdict} by NetworkPolicy")
    cni = sorted({n[4].split("-")[0] for n in g.nodes.values() if n[1] in ("Pod", "DaemonSet") and n[3] == "kube-system"
                  and any(c in n[4] for c in POLICY_CNI)})
    print("enforcement: " + (f"policy-capable CNI found ({', '.join(cni)})" if cni else
          "no policy-enforcing CNI found in kube-system: these policies may not be enforced at all"))
    if g.h(d_id)[0] == "fail":
        print(f"also: the destination is unhealthy: {line(g, d_id)}")

# --------------------------------------------------------------------------- CLI

def open_graph(a):
    ctx = a.context
    if a.db:
        path = a.db
    else:
        ctx = ctx or current_context()
        path = db_path(a, ctx)
    fresh = None
    if os.path.exists(path):
        g = Graph(path)
        age = time.time() - (g.meta.get("taken_at") or 0)
        live = g.meta.get("source") == "live"
        if a.refresh or (live and not a.no_refresh and age > a.max_age):
            fresh = g.meta
            g.db.close()
        else:
            return g
    elif a.no_refresh:
        raise SystemExit(f"kubegraph: no snapshot at {path}; run `snapshot` first")
    # (re-)snapshot with the scope the last snapshot used
    sa = argparse.Namespace(context=ctx or (fresh or {}).get("context"), from_dir=None, save=None, db=a.db,
                            namespace=(fresh or {}).get("namespaces") or getattr(a, "scope", None),
                            include=(fresh or {}).get("include"), exclude=(fresh or {}).get("exclude"),
                            timeout=a.timeout, jobs=a.jobs)
    print(f"kubegraph: {'refreshing' if fresh else 'taking'} snapshot of {sa.context or 'the current context'}", file=sys.stderr)
    return Graph(cmd_snapshot(sa, quiet=True))


GLOBAL_OPTS = ("--context", "--db", "--timeout", "--jobs", "--max-age")
GLOBAL_FLAGS = ("--refresh", "--no-refresh")


def hoist_globals(argv, commands):
    """Accept common options before the subcommand too (`run.sh --context X health`): move them after it."""
    i, front = 0, []
    while i < len(argv) and argv[i] not in commands:
        opt = argv[i].split("=", 1)[0]
        if opt in GLOBAL_OPTS and "=" not in argv[i] and i + 1 < len(argv):
            front += argv[i:i + 2]; i += 2
        elif opt in GLOBAL_OPTS or opt in GLOBAL_FLAGS:
            front.append(argv[i]); i += 1
        else:
            return argv
    if i == len(argv) or not front:
        return argv
    return [argv[i]] + front + argv[i + 1:]


def main(argv=None):
    p = argparse.ArgumentParser(prog="kubegraph", description=__doc__.split("\n\n")[0])
    p.add_argument("--version", action="version", version=f"kubegraph {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--context", help="kubectl context (default: the current one)")
    common.add_argument("--db", help="snapshot database (default: ~/.cache/kubegraph/<context>.db)")
    common.add_argument("--timeout", type=int, default=30, help="per-call kubectl timeout, seconds")
    common.add_argument("--jobs", type=int, default=12, help="parallel list calls")

    s = sub.add_parser("snapshot", parents=[common], help="collect the cluster (read-only) and build the graph")
    s.add_argument("-n", "--namespace", action="append", help="limit namespaced kinds to these namespaces")
    s.add_argument("--from", dest="from_dir", help="build from saved `kubectl get -o json` files instead")
    s.add_argument("--save", help="also save the sanitized lists here (input for --from, fixtures)")
    s.add_argument("--include", action="append", help=f"collect a resource normally skipped ({', '.join(sorted(SKIP))})")
    s.add_argument("--exclude", action="append", help="skip a resource type (e.g. secrets)")

    q = argparse.ArgumentParser(add_help=False, parents=[common])
    q.add_argument("-n", "--namespace", action="append")
    q.add_argument("--refresh", action="store_true", help="re-snapshot before answering")
    q.add_argument("--no-refresh", action="store_true", help="never re-snapshot (offline)")
    q.add_argument("--max-age", type=int, default=MAX_AGE, help="re-snapshot when older (seconds)")
    q.add_argument("--max-rows", type=int, default=None)
    q.add_argument("--all", action="store_true", help="lift section caps")
    q.add_argument("--repo", help="repository whose YAML manifests define the objects (default: the git checkout of the cwd)")
    q.add_argument("--no-repo", action="store_true", help="do not look for manifests")

    for name, fn, hlp in (("health", q_health, "unhealthy objects grouped by top owner"),
                          ("why", q_why, "cause chain for objects"),
                          ("show", q_show, "object cards"),
                          ("used-by", q_used_by, "what depends on an object"),
                          ("tree", q_tree, "ownership tree of objects or a namespace"),
                          ("find", q_find, "objects by name"),
                          ("events", q_events, "recent events"),
                          ("crds", q_crds, "custom resource types"),
                          ("overview", q_overview, "namespaces: pods, requests vs usage, unhealthy"),
                          ("waste", q_waste, "live cost waste: idle requests, unmounted disks, LBs, TTL-less envs, blockers"),
                          ("reach", q_reach, "NetworkPolicy reachability from one workload to another")):
        sp = sub.add_parser(name, parents=[q], help=hlp)
        sp.set_defaults(fn=fn)
        if name in ("why", "show", "used-by"):
            sp.add_argument("objects", nargs="+", help="kind/name, kind/ns/name, a printed id or a bare name")
        if name in ("tree", "events"):
            sp.add_argument("objects", nargs="*")
        if name == "find":
            sp.add_argument("pattern", nargs="?", default="")
            sp.add_argument("--kind")
            sp.add_argument("--unhealthy", action="store_true")
        if name == "why":
            sp.add_argument("--no-logs", action="store_true", help="do not fetch logs of the failing container")
            sp.add_argument("--tail", type=int, default=LOG_TAIL)
        if name == "used-by":
            sp.add_argument("--depth", type=int, default=4)
        if name == "waste":
            sp.add_argument("--env-ns", default=ENV_NS, help="globs of test-environment namespaces (comma-separated)")
            sp.add_argument("--min-age", type=float, default=4, help="hours before a TTL-less environment is listed")
        if name == "reach":
            sp.add_argument("source", help="pod, workload or Service the traffic comes from")
            sp.add_argument("dest", help="pod, workload or Service it goes to")
            sp.add_argument("--port", type=int, help="destination port (default: the Service's targetPort)")
            sp.add_argument("--protocol", default="TCP")

    a = p.parse_args(hoist_globals(sys.argv[1:] if argv is None else list(argv), sub.choices))
    if a.cmd == "snapshot":
        cmd_snapshot(a)
        return 0
    defaults = {"health": CAP_GROUPS, "find": CAP_FIND, "events": 30, "tree": 30, "used-by": 25, "overview": 30, "waste": 10}
    a.max_rows = a.max_rows or (10 ** 6 if a.all else defaults.get(a.cmd, CAP_GROUPS))
    a.cap = 10 ** 6 if a.all else CAP_LIST
    a.events = 10 ** 6 if a.all else CAP_EVENTS
    a.scope = None
    g = open_graph(a)
    if a.cmd in ("why", "show", "reach") and not a.no_repo:
        root = repo_root(a.repo)
        g.manifests = scan_manifests(root) if root else None
    try:
        a.fn(g, a)
    except QueryError as e:
        print(f"kubegraph: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
