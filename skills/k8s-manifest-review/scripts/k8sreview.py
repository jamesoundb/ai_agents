#!/usr/bin/env python3
"""
k8sreview.py — efficiency and hygiene review of Kubernetes manifests for build/test workloads.

Reads plain YAML (multi-document), `helm template` output or `kustomize build` output, applies
deterministic rules and a per-repo policy (k8s-review.json: build size tiers, TTL annotation,
spot pool selector, replica caps, required labels), and prints a findings table with an exit-code
gate for CI.

  k8sreview.py PATH... [--policy k8s-review.json] [--tiers build-tiers.json] [--fail-on high]
                       [--build] [--no-tools] [--json] [--rules]
  helm template . | k8sreview.py -            # stdin
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from yamlload import load_all  # noqa: E402

SEVERITIES = ["info", "low", "medium", "high", "critical"]
DEFAULT_POLICY = {
    "tiers": {"small": {"cpu": 1, "memory_gib": 2}, "medium": {"cpu": 2, "memory_gib": 4}, "large": {"cpu": 4, "memory_gib": 8}},
    "default_tier": "medium",
    "tier_label": "build.company.io/tier",
    "build_namespaces": [],            # namespaces whose workloads are builds/test envs; empty + --build = all
    "ttl_annotation": "janitor/ttl",    # kube-janitor style TTL, e.g. "8h"
    "max_test_replicas": 2,
    "spot_selector": {"cloud.google.com/gke-spot": "true"},
    "required_labels": ["app"],
    "max_containers": 3,
    "disabled_rules": [],
    "severity_overrides": {},
}
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Pod", "ReplicaSet"}
GIB = 1024 ** 3


def cpu(q):
    if q is None:
        return None
    q = str(q)
    return float(q[:-1]) / 1000 if q.endswith("m") else float(q)


def mem(q):
    if q is None:
        return None
    q = str(q)
    units = {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}
    for u, mult in units.items():
        if q.endswith(u):
            return float(q[:-len(u)]) * mult
    return float(q)


class Report:
    def __init__(self, policy):
        self.policy = policy
        self.findings = []
        self.tools = []

    def add(self, rule, sev, file, line, obj, message, fix=None):
        if rule in self.policy["disabled_rules"]:
            return
        sev = self.policy["severity_overrides"].get(rule, sev)
        self.findings.append({"rule": rule, "severity": sev, "file": file, "line": line, "object": obj, "message": message, "fix": fix})


def pod_spec_of(doc):
    kind = doc.get("kind")
    spec = doc.get("spec") or {}
    if kind == "Pod":
        return spec
    if kind == "CronJob":
        return (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec")
    if kind in WORKLOAD_KINDS:
        return (spec.get("template") or {}).get("spec")
    return None


def review_doc(doc, file, line, rep, is_build):
    P = rep.policy
    if not isinstance(doc, dict) or "kind" not in doc:
        return
    kind = doc.get("kind")
    meta = doc.get("metadata") or {}
    name = meta.get("name", "?")
    ns = meta.get("namespace")
    obj = f"{kind}/{name}"
    labels = meta.get("labels") or {}
    ann = meta.get("annotations") or {}
    spec = doc.get("spec") or {}
    build = is_build or (ns in P["build_namespaces"]) or (P["tier_label"] in labels)

    if kind in WORKLOAD_KINDS and ns is None:
        rep.add("COR001", "medium", file, line, obj, "no metadata.namespace; the object lands wherever the client points", "set namespace explicitly")
    missing = [l for l in P["required_labels"] if l not in labels]
    if kind in WORKLOAD_KINDS | {"Service", "ConfigMap", "Secret"} and missing:
        rep.add("COR002", "low", file, line, obj, f"missing required label(s): {', '.join(missing)}", "add labels for ownership and cost allocation")

    ps = pod_spec_of(doc)
    if ps is None:
        return
    containers = (ps.get("containers") or []) + (ps.get("initContainers") or [])
    tier_name = labels.get(P["tier_label"]) or P["default_tier"]
    tier = P["tiers"].get(tier_name)
    total_cpu, total_mem = 0.0, 0.0
    for c in ps.get("containers") or []:
        cname = c.get("name", "?")
        res = c.get("resources") or {}
        req, lim = res.get("requests") or {}, res.get("limits") or {}
        rc, rm_, lc, lm = cpu(req.get("cpu")), mem(req.get("memory")), cpu(lim.get("cpu")), mem(lim.get("memory"))
        if rc is None or rm_ is None:
            rep.add("EFF001", "high", file, line, obj, f"container {cname} has no cpu/memory requests (BestEffort/Burstable)", "set requests from the tier table; the scheduler and autoscaler size nodes from requests")
        else:
            total_cpu += rc; total_mem += rm_
        if lm is None:
            rep.add("EFF002", "medium", file, line, obj, f"container {cname} has no memory limit", "set memory limit = memory request so a leak cannot evict neighbours")
        elif rm_ is not None and abs(lm - rm_) > 1:
            rep.add("EFF003", "low", file, line, obj, f"container {cname} memory limit ({lm / GIB:.1f} GiB) differs from request ({rm_ / GIB:.1f} GiB)", "memory limit = request gives Guaranteed QoS and predictable packing")
        if rc and lc and lc / rc >= 8:
            rep.add("EFF003", "info", file, line, obj, f"container {cname} cpu limit is {lc / rc:.0f}x its request", "a large gap usually means the request is understated; use measured p95 x headroom")
        if "ephemeral-storage" not in req and build:
            rep.add("EFF013", "low", file, line, obj, f"container {cname} has no ephemeral-storage request", "build caches and checkouts consume disk; request e.g. 10Gi so the scheduler accounts for it")
        img = c.get("image") or ""
        if img and (":" not in img.split("/")[-1] or img.endswith(":latest")):
            rep.add("EFF012", "low", file, line, obj, f"container {cname} image '{img}' is untagged or :latest", "pin a tag or digest; unpinned images defeat the node image cache and reproducibility")
        if (c.get("imagePullPolicy") == "Always") and img and ":" in img.split("/")[-1] and not img.endswith(":latest"):
            rep.add("COR005", "low", file, line, obj, f"container {cname} pulls a pinned image with imagePullPolicy Always", "IfNotPresent keeps the node cache useful")
        sc = c.get("securityContext") or {}
        if sc.get("privileged"):
            rep.add("COR004", "high", file, line, obj, f"container {cname} is privileged", "drop privileged; use a specific capability or a sandboxed builder (kaniko/buildkit rootless)")
    if tier and total_cpu and (total_cpu > tier["cpu"] + 1e-9 or total_mem > tier["memory_gib"] * GIB + 1):
        rep.add("EFF004", "high", file, line, obj, f"pod requests {total_cpu:g} cpu / {total_mem / GIB:.1f} GiB exceed tier '{tier_name}' ({tier['cpu']} cpu / {tier['memory_gib']} GiB)",
                f"lower requests or label the workload {P['tier_label']}=<bigger tier> with justification")
    elif tier and total_cpu and P["tier_label"] not in labels and build:
        rep.add("EFF004", "info", file, line, obj, f"no {P['tier_label']} label; evaluated against default tier '{tier_name}'", "label build workloads with their tier")
    if len(ps.get("containers") or []) > P["max_containers"]:
        rep.add("EFF015", "low", file, line, obj, f"{len(ps['containers'])} containers in one pod", "sidecars in build pods cost requests on every run; keep only what the build needs")
    if ps.get("hostNetwork") or ps.get("hostPID"):
        rep.add("COR004", "high", file, line, obj, "hostNetwork/hostPID enabled", "remove unless the workload truly needs node namespaces")
    for v in ps.get("volumes") or []:
        if "hostPath" in v:
            rep.add("COR004", "high", file, line, obj, f"hostPath volume '{v.get('name')}'", "use emptyDir or a PVC")
        if "persistentVolumeClaim" in v and build:
            rep.add("EFF011", "low", file, line, obj, f"PVC volume '{v.get('name')}' in a build/test workload", "PVCs outlive the pod and block scale-down; prefer emptyDir or a cache bucket")
        if "emptyDir" in v and isinstance(v["emptyDir"], dict) and "sizeLimit" not in v["emptyDir"]:
            rep.add("EFF013", "info", file, line, obj, f"emptyDir '{v.get('name')}' without sizeLimit", "cap it so a runaway build cannot fill the node disk")
    # placement on spot
    if build and P.get("spot_selector"):
        sel = ps.get("nodeSelector") or {}
        tol = ps.get("tolerations") or []
        aff = json.dumps(ps.get("affinity") or {})
        on_spot = any(sel.get(k) == v for k, v in P["spot_selector"].items()) or any(k in aff for k in P["spot_selector"]) or any(t.get("key") in P["spot_selector"] for t in tol)
        if not on_spot:
            rep.add("EFF010", "medium", file, line, obj, "not steered to the spot build pool", f"nodeSelector {json.dumps(P['spot_selector'])} + toleration for the spot taint (add on-demand fallback via affinity for critical builds)")
    # lifecycle
    if kind == "Job":
        if "ttlSecondsAfterFinished" not in spec:
            rep.add("EFF005", "high", file, line, obj, "Job has no ttlSecondsAfterFinished", "ttlSecondsAfterFinished: 600 so finished pods release their requests and are garbage-collected")
        if "activeDeadlineSeconds" not in spec:
            rep.add("EFF006", "medium", file, line, obj, "Job has no activeDeadlineSeconds", "cap runtime (e.g. 2h) so a hung build cannot hold a node all night")
        if (spec.get("backoffLimit") or 6) > 3:
            rep.add("EFF007", "low", file, line, obj, f"backoffLimit {spec.get('backoffLimit', 6)} retries a failing build", "backoffLimit: 1 for tests (a failing test does not pass on retry)")
    if kind == "CronJob":
        js = spec.get("jobTemplate", {}).get("spec", {})
        if "ttlSecondsAfterFinished" not in js:
            rep.add("EFF005", "medium", file, line, obj, "CronJob's jobTemplate has no ttlSecondsAfterFinished", "add it; successfulJobsHistoryLimit only limits Job objects")
        if spec.get("concurrencyPolicy", "Allow") == "Allow":
            rep.add("COR006", "low", file, line, obj, "concurrencyPolicy Allow lets runs pile up", "Forbid or Replace for scheduled tests")
    if kind in ("Deployment", "StatefulSet") and build:
        if P["ttl_annotation"] and P["ttl_annotation"] not in ann and "janitor/expires" not in ann:
            rep.add("EFF008", "high", file, line, obj, f"test environment has no teardown TTL (annotation {P['ttl_annotation']} or janitor/expires)", f"annotate {P['ttl_annotation']}: \"8h\" (kube-janitor) or add a teardown step to the build; environments left up are the largest waste item")
        if (spec.get("replicas") or 1) > P["max_test_replicas"]:
            rep.add("EFF009", "medium", file, line, obj, f"replicas {spec.get('replicas')} exceed the test cap {P['max_test_replicas']}", "tests rarely need more than one or two replicas; scale-out belongs in load tests only")
    if kind == "Deployment":
        for c in ps.get("containers") or []:
            if not c.get("readinessProbe") and not build:
                rep.add("COR003", "low", file, line, obj, f"container {c.get('name')} has no readinessProbe", "add a probe so rollouts and Services only route to ready pods")


def review_text(text, file, rep, is_build):
    try:
        docs, lines = load_all(text)
    except Exception as e:  # noqa: BLE001
        rep.add("COR000", "high", file, 1, "-", f"YAML parse error: {e}", "fix the YAML; if this is a Helm template, render it first (helm template)")
        return 0
    count = 0
    for doc, line in zip(docs, lines):
        if isinstance(doc, dict) and doc.get("kind") == "List":
            for it in doc.get("items") or []:
                review_doc(it, file, line, rep, is_build)
                count += 1
        else:
            review_doc(doc, file, line, rep, is_build)
            count += 1
    return count


def external_tools(files, rep):
    """Schema validation: kubeconform (offline, preferred) else kubectl client dry-run (needs a reachable cluster)."""
    kf, kc = shutil.which("kubeconform"), shutil.which("kubectl")
    real = [f for f in files if f != "-"]
    if not real:
        return
    if kf:
        try:
            p = subprocess.run([kf, "-strict", "-ignore-missing-schemas", "-summary"] + real, capture_output=True, text=True, timeout=120)
            rep.tools.append({"tool": "kubeconform", "ok": p.returncode == 0, "output": (p.stdout + p.stderr).strip()[:2000]})
        except (OSError, subprocess.TimeoutExpired) as e:
            rep.tools.append({"tool": "kubeconform", "ok": False, "output": str(e)})
        return
    if not kc:
        rep.tools.append({"tool": "schema validation", "ok": None, "output": "neither kubeconform nor kubectl installed; skipped"})
        return
    for f in real:
        try:
            p = subprocess.run([kc, "--request-timeout=10s", "apply", "--dry-run=client", "--validate=false", "-f", f, "-o", "name"], capture_output=True, text=True, timeout=25)
            if p.returncode != 0 and re.search(r"connection refused|timeout|no such host|Unable to connect|i/o timeout|context deadline", p.stderr, re.I):
                rep.tools.append({"tool": "kubectl apply --dry-run=client", "file": f, "ok": None, "output": "no reachable cluster; install kubeconform for offline schema validation"})
                return
            rep.tools.append({"tool": "kubectl apply --dry-run=client", "file": f, "ok": p.returncode == 0, "output": (p.stderr or p.stdout).strip()[:1500]})
        except subprocess.TimeoutExpired:
            rep.tools.append({"tool": "kubectl apply --dry-run=client", "file": f, "ok": None, "output": "timed out reaching the cluster; install kubeconform for offline schema validation"})
            return
        except OSError as e:
            rep.tools.append({"tool": "kubectl apply --dry-run=client", "file": f, "ok": False, "output": str(e)})


def load_policy(path, tiers_path, start_dirs):
    P = json.loads(json.dumps(DEFAULT_POLICY))
    cands = [path] if path else []
    for d in start_dirs:
        cands += [os.path.join(d, "k8s-review.json"), os.path.join(d, ".k8s-review.json")]
    for c in cands:
        if c and os.path.exists(c):
            P.update(json.load(open(c)))
            P["_policy_file"] = c
            break
    tcands = [tiers_path] if tiers_path else []
    for d in start_dirs:
        tcands.append(os.path.join(d, "build-tiers.json"))
    if P.get("tiers_file"):
        tcands.insert(0, P["tiers_file"])
    for c in tcands:
        if c and os.path.exists(c):
            data = json.load(open(c))
            P["tiers"] = data.get("tiers", data)
            P["_tiers_file"] = c
            break
    return P


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="YAML files, directories, or - for stdin")
    ap.add_argument("--policy")
    ap.add_argument("--tiers", help="build-tiers.json from gke-cost-discovery")
    ap.add_argument("--fail-on", default="high", choices=SEVERITIES)
    ap.add_argument("--build", action="store_true", help="treat every workload as a build/test workload")
    ap.add_argument("--no-tools", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--rules", action="store_true")
    a = ap.parse_args(argv)
    if a.rules:
        for rid, sev in sorted(set(re.findall(r'rep\.add\("([A-Z]+\d+)", "(\w+)"', open(__file__).read()))):
            print(f"{rid}\t{sev}")
        return 0
    files = []
    for p in a.paths:
        if p == "-":
            files.append("-")
        elif os.path.isdir(p):
            files += sorted(f for f in glob.glob(os.path.join(p, "**", "*.y*ml"), recursive=True) if "/templates/" not in f and "/charts/" not in f)
        else:
            files.append(p)
    start_dirs = list(dict.fromkeys(os.path.dirname(os.path.abspath(f)) if f != "-" else os.getcwd() for f in files)) + [os.getcwd()]
    P = load_policy(a.policy, a.tiers, start_dirs)
    rep = Report(P)
    objects = 0
    for f in files:
        text = sys.stdin.read() if f == "-" else open(f, encoding="utf-8").read()
        if "{{" in text and f != "-":
            rep.add("COR000", "medium", f, 1, "-", "file contains Go template syntax (Helm); reviewing the rendered output is required", "helm template <chart> | k8sreview.py -")
            continue
        objects += review_text(text, f, rep, a.build)
    if not a.no_tools:
        external_tools(files, rep)
    seen, uniq = set(), []
    for x in rep.findings:
        k = (x["rule"], x["file"], x["line"], x["message"])
        if k not in seen:
            seen.add(k); uniq.append(x)
    rep.findings = sorted(uniq, key=lambda x: (-SEVERITIES.index(x["severity"]), x["file"], x["line"]))
    gate = SEVERITIES.index(a.fail_on)
    failing = [x for x in rep.findings if SEVERITIES.index(x["severity"]) >= gate] or [t for t in rep.tools if t["ok"] is False]
    if a.json:
        print(json.dumps({"objects": objects, "findings": rep.findings, "tools": rep.tools, "policy_file": P.get("_policy_file"), "tiers_file": P.get("_tiers_file"), "fail": bool(failing)}, indent=1))
        return 1 if failing else 0
    counts = defaultdict(int)
    for x in rep.findings:
        counts[x["severity"]] += 1
    print(f"## Kubernetes manifest review: {objects} object(s) in {len(files)} file(s)")
    print("Policy: " + (P.get("_policy_file") or "defaults") + "; tiers: " + (P.get("_tiers_file") or "policy defaults") + f"; tier label {P['tier_label']}")
    print("Findings: " + (", ".join(f"{s} {counts[s]}" for s in reversed(SEVERITIES) if counts[s]) or "none"))
    if rep.findings:
        print("\n| severity | rule | location | object | finding | fix |\n|---|---|---|---|---|---|")
        for x in rep.findings:
            print(f"| {x['severity']} | {x['rule']} | {x['file']}:{x['line']} | {x['object']} | {x['message']} | {x['fix'] or ''} |")
    if rep.tools:
        print("\n### Tools")
        for t in rep.tools:
            st = "ok" if t["ok"] else ("skipped" if t["ok"] is None else "FAILED")
            print(f"- {t['tool']}{(' [' + t['file'] + ']') if t.get('file') else ''}: {st}" + (f"\n```\n{t['output']}\n```" if t["ok"] is False and t["output"] else ""))
    print(f"\nGate (--fail-on {a.fail_on}): " + ("FAIL" if failing else "PASS"))
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
