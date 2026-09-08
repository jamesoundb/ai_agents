#!/usr/bin/env python3
"""
analyze.py — turn a collect.sh directory into a GKE cost-efficiency report (stdlib only).

  analyze.py DISCOVERY_DIR [--build-namespaces ns1,ns2] [--headroom 1.25] [--json]
             [--pricing pricing.json] [--tiers-out build-tiers.json] [--idle-hours 4]

Waste model
  unused      = requested - p95 usage        (manifest problem: requests too high)
  unallocated = allocatable - requested      (cluster problem: node pools / bin packing / scale-down)
Both are reported in cores, GiB and approximate $/month, with the top offenders.
Missing inputs are listed at the end so the operator knows what to provide next.
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
GIB = 1024 ** 3


# ----------------------------------------------------------------------------------------------
# Kubernetes quantity parsing
# ----------------------------------------------------------------------------------------------
def cpu(q):
    if q is None:
        return 0.0
    q = str(q)
    return float(q[:-1]) / 1000 if q.endswith("m") else float(q)


def mem(q):
    if q is None:
        return 0.0
    q = str(q)
    units = {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}
    for u, mult in units.items():
        if q.endswith(u):
            return float(q[:-len(u)]) * mult
    return float(q)


def load_json(d, name):
    p = os.path.join(d, name)
    if not os.path.exists(p):
        return None
    with open(p) as f:
        txt = f.read()
    try:
        return json.loads(txt)
    except ValueError:
        # time-series files may hold several concatenated pages
        objs, dec, i = [], json.JSONDecoder(), 0
        while i < len(txt):
            while i < len(txt) and txt[i].isspace():
                i += 1
            if i >= len(txt):
                break
            o, j = dec.raw_decode(txt, i)
            objs.append(o)
            i = j
        return {"timeSeries": [s for o in objs for s in o.get("timeSeries", [])]}


def series_points(ts):
    out = []
    for p in ts.get("points", []):
        v = p.get("value", {})
        val = v.get("doubleValue", v.get("int64Value"))
        if val is not None:
            out.append(float(val))
    return out


def pct(vals, q):
    if not vals:
        return 0.0
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round((len(s) - 1) * q))))
    return s[k]


def mean(vals):
    return sum(vals) / len(vals) if vals else 0.0


# ----------------------------------------------------------------------------------------------
# Analysis
# ----------------------------------------------------------------------------------------------
def pod_spec_of_workload(it):
    spec = it.get("spec") or {}
    if it.get("kind") == "CronJob":
        return (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
    return (spec.get("template") or {}).get("spec") or {}


def load_billing(d):
    """Optional billing.csv in the discovery dir: namespace,cost_month (from the GKE cost-allocation
    billing export, see reference/metrics.md). Actual dollars override list-price estimates."""
    p = os.path.join(d, "billing.csv")
    if not os.path.exists(p):
        return {}
    out = {}
    import csv
    with open(p, newline="") as f:
        for row in csv.DictReader(f):
            ns = (row.get("namespace") or row.get("labels.k8s-namespace") or "").strip()
            try:
                out[ns] = out.get(ns, 0.0) + float(row.get("cost_month") or row.get("cost") or 0)
            except ValueError:
                continue
    return {k: v for k, v in out.items() if k}


def analyze(d, build_ns, headroom, pricing, idle_hours):
    rep = {"missing": [], "cluster": {}, "snapshot": {}, "usage": {}, "autoscaler": {}, "teamcity": {}, "recommendations": [], "tiers": None}
    for fn in sorted(os.listdir(d)):
        if fn.startswith("MISSING-"):
            rep["missing"].append({"input": fn[8:], "reason": open(os.path.join(d, fn)).read().strip()})
    meta = load_json(d, "meta.json") or {}
    if not build_ns and meta.get("build_namespaces"):
        build_ns = [x for x in meta["build_namespaces"].split(",") if x]
    build_ns = set(build_ns or [])
    now = datetime.now(timezone.utc)
    price = pricing

    def node_price(machine_type, spot):
        fam = (machine_type or "").split("-")[0]
        p = price["families"].get(fam, price["default"])
        disc = (1 - price["spot_discount"]) if spot else 1.0
        return p["vcpu_hour"] * disc, p["gb_hour"] * disc

    # ---- cluster / node pools ----------------------------------------------------------------
    cluster = load_json(d, "cluster.json")
    pools = []
    if cluster:
        for np_ in cluster.get("nodePools", []):
            cfg, asc = np_.get("config", {}), np_.get("autoscaling", {})
            pools.append({"name": np_.get("name"), "machine_type": cfg.get("machineType"), "spot": bool(cfg.get("spot") or cfg.get("preemptible")),
                          "autoscaling": bool(asc.get("enabled")), "min": asc.get("minNodeCount", asc.get("totalMinNodeCount", 0)),
                          "max": asc.get("maxNodeCount", asc.get("totalMaxNodeCount")), "initial": np_.get("initialNodeCount"),
                          "locations": len(np_.get("locations", []) or [])})
        rep["cluster"] = {"name": cluster.get("name"), "location": cluster.get("location"), "autopilot": bool((cluster.get("autopilot") or {}).get("enabled")),
                          "current_nodes": cluster.get("currentNodeCount"), "node_pools": pools,
                          "vpa": bool((cluster.get("verticalPodAutoscaling") or {}).get("enabled")),
                          "autoscaling_profile": (cluster.get("autoscaling") or {}).get("autoscalingProfile", "BALANCED")}
        for p in pools:
            burst_like = any(w in (p["name"] or "").lower() for w in ("build", "agent", "ci", "test", "runner", "spot"))
            if p["autoscaling"] and (p["min"] or 0) > 0 and burst_like:
                rep["recommendations"].append({"area": "cluster", "priority": "medium", "item": f"node pool {p['name']} keeps min {p['min']} node(s) per zone x {p['locations'] or 1} zone(s) warm ({p['machine_type']}); burst-only build pools can run min 0 (builds queue ~1-2 min while a node boots)"})
            if not p["spot"] and (build_ns and ("build" in (p["name"] or "") or "agent" in (p["name"] or "") or "ci" in (p["name"] or ""))):
                rep["recommendations"].append({"area": "cluster", "priority": "high", "item": f"node pool {p['name']} looks like a build pool but is on-demand; a spot pool (with on-demand fallback) cuts its price by ~{int(price['spot_discount'] * 100)}%"})
        if rep["cluster"]["autoscaling_profile"] != "OPTIMIZE_UTILIZATION":
            rep["recommendations"].append({"area": "cluster", "priority": "medium", "item": "cluster autoscaler profile is BALANCED; OPTIMIZE_UTILIZATION scales down sooner and packs tighter (fine for build clusters)"})
        if not rep["cluster"]["vpa"]:
            rep["recommendations"].append({"area": "cluster", "priority": "low", "item": "Vertical Pod Autoscaling is off; enabling it in recommendation-only mode gives per-container request suggestions for free"})

    # ---- snapshot: nodes and pods -----------------------------------------------------------------
    nodes = load_json(d, "nodes.json")
    pods = load_json(d, "pods.json")
    node_info = {}
    if nodes:
        for n in nodes.get("items", []):
            labels = n["metadata"].get("labels", {})
            alloc = n["status"].get("allocatable", {})
            node_info[n["metadata"]["name"]] = {
                "pool": labels.get("cloud.google.com/gke-nodepool", "?"), "machine_type": labels.get("node.kubernetes.io/instance-type", "?"),
                "spot": labels.get("cloud.google.com/gke-spot") == "true" or labels.get("cloud.google.com/gke-preemptible") == "true",
                "alloc_cpu": cpu(alloc.get("cpu")), "alloc_mem": mem(alloc.get("memory")), "req_cpu": 0.0, "req_mem": 0.0, "pods": 0}
    snap = {"namespaces": defaultdict(lambda: {"req_cpu": 0.0, "req_mem": 0.0, "pods": 0, "no_requests": 0, "completed": 0}),
            "no_requests": [], "ratio_issues": [], "long_running_build_pods": [], "lingering": [], "blockers": []}
    if pods:
        for p in pods.get("items", []):
            ns, name = p["metadata"]["namespace"], p["metadata"]["name"]
            phase = p["status"].get("phase")
            node = p["spec"].get("nodeName")
            rc, rm_, lc, lm = 0.0, 0.0, 0.0, 0.0
            missing = False
            for c in p["spec"].get("containers", []):
                res = c.get("resources", {})
                req, lim = res.get("requests", {}), res.get("limits", {})
                if "cpu" not in req or "memory" not in req:
                    missing = True
                rc += cpu(req.get("cpu")); rm_ += mem(req.get("memory")); lc += cpu(lim.get("cpu")); lm += mem(lim.get("memory"))
            s = snap["namespaces"][ns]
            s["pods"] += 1
            if phase in ("Succeeded", "Failed"):
                s["completed"] += 1
                snap["lingering"].append(f"{ns}/{name} ({phase})")
                continue
            s["req_cpu"] += rc; s["req_mem"] += rm_
            system = ns in ("kube-system", "gke-managed-system", "gmp-system", "gke-gmp-system", "config-management-system")
            if missing and not system:  # static/system pods are managed by GKE; only user workloads are actionable
                s["no_requests"] += 1
                snap["no_requests"].append(f"{ns}/{name}")
            if rc and lc and lc / rc >= 4 and not system:
                snap["ratio_issues"].append(f"{ns}/{name}: cpu limit {lc:g} is {lc / rc:.0f}x request {rc:g}")
            if node in node_info and phase == "Running":
                node_info[node]["req_cpu"] += rc; node_info[node]["req_mem"] += rm_; node_info[node]["pods"] += 1
            ann = p["metadata"].get("annotations", {})
            if ann.get("cluster-autoscaler.kubernetes.io/safe-to-evict") == "false":
                snap["blockers"].append(f"{ns}/{name}: safe-to-evict=false")
            if not p["metadata"].get("ownerReferences") and ns not in ("kube-system",):
                snap["blockers"].append(f"{ns}/{name}: bare pod (no controller) blocks scale-down")
            if ns in build_ns and p["status"].get("startTime"):
                started = datetime.fromisoformat(p["status"]["startTime"].replace("Z", "+00:00"))
                age_h = (now - started).total_seconds() / 3600
                if age_h > idle_hours:
                    snap["long_running_build_pods"].append(f"{ns}/{name}: running {age_h:.0f}h, requests {rc:g} cpu / {rm_ / GIB:.1f} GiB")
    unalloc_now = {"cpu": 0.0, "mem": 0.0, "alloc_cpu": 0.0, "alloc_mem": 0.0, "cost_month": 0.0, "nodes": []}
    for name, n in node_info.items():
        uc, um = max(0.0, n["alloc_cpu"] - n["req_cpu"]), max(0.0, n["alloc_mem"] - n["req_mem"])
        vc, gb = node_price(n["machine_type"], n["spot"])
        cost = (uc * vc + um / GIB * gb) * price["hours_per_month"]
        unalloc_now["cpu"] += uc; unalloc_now["mem"] += um; unalloc_now["alloc_cpu"] += n["alloc_cpu"]; unalloc_now["alloc_mem"] += n["alloc_mem"]; unalloc_now["cost_month"] += cost
        unalloc_now["nodes"].append({"node": name, "pool": n["pool"], "type": n["machine_type"], "spot": n["spot"], "pods": n["pods"],
                                     "cpu_req_pct": (n["req_cpu"] / n["alloc_cpu"] * 100) if n["alloc_cpu"] else 0,
                                     "mem_req_pct": (n["req_mem"] / n["alloc_mem"] * 100) if n["alloc_mem"] else 0, "unalloc_cost_month": cost})
    unalloc_now["nodes"].sort(key=lambda x: -x["unalloc_cost_month"])
    rep["snapshot"] = {"namespaces": {k: dict(v) for k, v in snap["namespaces"].items()}, "no_requests": snap["no_requests"][:30],
                       "ratio_issues": snap["ratio_issues"][:30], "long_running_build_pods": snap["long_running_build_pods"][:30],
                       "lingering_completed_pods": len(snap["lingering"]), "lingering_examples": snap["lingering"][:10],
                       "scale_down_blockers": snap["blockers"][:30], "unallocated_now": unalloc_now if node_info else None}
    if snap["no_requests"]:
        rep["recommendations"].append({"area": "manifests", "priority": "high", "item": f"{len(snap['no_requests'])} running pod(s) have containers without cpu/memory requests (BestEffort/Burstable): the scheduler cannot pack them and the autoscaler cannot size for them; add requests or a namespace LimitRange default"})
    if snap["lingering"]:
        rep["recommendations"].append({"area": "manifests", "priority": "medium", "item": f"{len(snap['lingering'])} completed/failed pod(s) still exist; set ttlSecondsAfterFinished on Jobs and clean up test environments after the run"})
    if snap["long_running_build_pods"]:
        rep["recommendations"].append({"area": "manifests", "priority": "high", "item": f"{len(snap['long_running_build_pods'])} build-namespace pod(s) running longer than {idle_hours}h: likely test environments left up; add activeDeadlineSeconds / a teardown step / a kube-janitor TTL (janitor/ttl annotation or a default-TTL rule)"})
    if snap["blockers"]:
        rep["recommendations"].append({"area": "cluster", "priority": "medium", "item": f"{len(snap['blockers'])} pod(s) block node scale-down (safe-to-evict=false or bare pods); move them to a small on-demand system pool"})

    # ---- workloads: jobs/deployments in build namespaces ---------------------------------------------
    wl = load_json(d, "workloads.json")
    if wl:
        jobs_no_ttl, jobs_no_deadline, test_envs = [], [], []
        for it in wl.get("items", []):
            kind, ns, name = it.get("kind"), it["metadata"]["namespace"], it["metadata"]["name"]
            spec = it.get("spec", {})
            if kind == "Job":
                if "ttlSecondsAfterFinished" not in spec:
                    jobs_no_ttl.append(f"{ns}/{name}")
                if "activeDeadlineSeconds" not in spec:
                    jobs_no_deadline.append(f"{ns}/{name}")
            if kind == "Deployment" and (not build_ns or ns in build_ns) and (spec.get("replicas") or 0) > 0 and it["metadata"].get("creationTimestamp"):
                created = datetime.fromisoformat(it["metadata"]["creationTimestamp"].replace("Z", "+00:00"))
                age_h = (now - created).total_seconds() / 3600
                if ns in build_ns and age_h > idle_hours:
                    test_envs.append(f"{ns}/{name}: {spec.get('replicas')} replica(s), up {age_h / 24:.1f} d")
        rep["snapshot"]["jobs_without_ttl"] = jobs_no_ttl[:30]
        rep["snapshot"]["jobs_without_deadline"] = jobs_no_deadline[:30]
        rep["snapshot"]["test_environments_up"] = test_envs[:30]
        if jobs_no_ttl:
            rep["recommendations"].append({"area": "manifests", "priority": "medium", "item": f"{len(jobs_no_ttl)} Job(s) without ttlSecondsAfterFinished"})
        if test_envs:
            rep["recommendations"].append({"area": "manifests", "priority": "high", "item": f"{len(test_envs)} Deployment(s) in build namespaces older than {idle_hours}h with replicas > 0: test environments that were never torn down"})
    # kube-janitor: is it deployed, is it live (not --dry-run), does it cover the build namespaces?
    janitor = {"deployed": False, "dry_run": False, "args": [], "exclude_namespaces": None, "include_namespaces": None, "include_resources": None, "rules_file": None, "interval": None}
    if wl:
        for it in wl.get("items", []):
            if it.get("kind") in ("Deployment", "CronJob") and "kube-janitor" in it["metadata"]["name"]:
                janitor["deployed"] = True
                ps = pod_spec_of_workload(it)
                for c in (ps.get("containers") or []):
                    args = (c.get("args") or []) + (c.get("command") or [])
                    janitor["args"] += args
                    for a_ in args:
                        if a_ == "--dry-run":
                            janitor["dry_run"] = True
                        for key in ("exclude-namespaces", "include-namespaces", "include-resources", "rules-file", "interval"):
                            if a_.startswith(f"--{key}="):
                                janitor[key.replace("-", "_")] = a_.split("=", 1)[1]
        unannotated = []
        for it in wl.get("items", []):
            ns = it["metadata"]["namespace"]
            if it.get("kind") in ("Deployment", "StatefulSet", "Job") and ns in build_ns:
                ann = it["metadata"].get("annotations") or {}
                if "janitor/ttl" not in ann and "janitor/expires" not in ann and "kube-janitor" not in it["metadata"]["name"]:
                    unannotated.append(f"{ns}/{it.get('kind')}/{it['metadata']['name']}")
        janitor["unannotated_in_build_namespaces"] = unannotated
        if not janitor["deployed"]:
            rep["recommendations"].append({"area": "guardrails", "priority": "high", "item": "kube-janitor is not deployed in this cluster (no Deployment/CronJob named kube-janitor): nothing expires test environments; deploy it or use k8s-guardrails --standalone-janitor"})
        else:
            if janitor["dry_run"]:
                rep["recommendations"].append({"area": "guardrails", "priority": "high", "item": "kube-janitor runs with --dry-run: it logs what it would delete but deletes nothing; remove the flag once the rules are trusted"})
            inc, exc = janitor["include_namespaces"], janitor["exclude_namespaces"]
            for ns in sorted(build_ns):
                if (inc and inc != "all" and ns not in inc.split(",")) or (exc and ns in exc.split(",")):
                    rep["recommendations"].append({"area": "guardrails", "priority": "high", "item": f"kube-janitor does not cover namespace {ns} (include/exclude flags); test environments there never expire"})
            res = janitor["include_resources"]
            if res and res != "all" and not any(r in res for r in ("deployments", "all")):
                rep["recommendations"].append({"area": "guardrails", "priority": "medium", "item": f"kube-janitor --include-resources={res} does not include deployments; test-environment Deployments are never cleaned"})
            if unannotated and not janitor["rules_file"]:
                rep["recommendations"].append({"area": "guardrails", "priority": "high", "item": f"{len(unannotated)} workload(s) in build namespaces have no janitor/ttl or janitor/expires annotation and kube-janitor has no rules file: they will never expire; add a default-TTL rule (k8s-guardrails emits one) or require the annotation in the build template"})
            elif unannotated:
                rep["recommendations"].append({"area": "guardrails", "priority": "medium", "item": f"{len(unannotated)} build-namespace workload(s) lack a TTL annotation; confirm the kube-janitor rules file ({janitor['rules_file']}) gives them a default TTL"})
    rep["snapshot"]["kube_janitor"] = janitor
    quotas = load_json(d, "quotas.json")
    if quotas is not None and build_ns:
        covered = {it["metadata"]["namespace"] for it in quotas.get("items", []) if it.get("kind") == "LimitRange"}
        for ns in sorted(build_ns - covered):
            rep["recommendations"].append({"area": "guardrails", "priority": "medium", "item": f"namespace {ns} has no LimitRange (default requests/limits and max per container); add one from the proposed tiers"})

    # ---- time series: unused (requested - used) and unallocated over time ----------------------------
    def load_ts(name):
        obj = load_json(d, f"ts_{name}.json")
        return obj.get("timeSeries", []) if obj else None

    cpu_util, mem_util = load_ts("cpu_req_util"), load_ts("mem_req_util")
    cpu_req, mem_req = load_ts("cpu_req_cores"), load_ts("mem_req_bytes")
    usage = {}
    if cpu_util and cpu_req:
        def key(ts):
            r, m = ts.get("resource", {}).get("labels", {}), ts.get("metadata", {}).get("systemLabels", {})
            return (r.get("namespace_name", "?"), m.get("top_level_controller_name") or r.get("pod_name", "?"), r.get("container_name", "?"))
        util_c = {key(t): series_points(t) for t in cpu_util}
        util_m = {key(t): series_points(t) for t in (mem_util or [])}
        req_c = {key(t): series_points(t) for t in cpu_req}
        req_m = {key(t): series_points(t) for t in (mem_req or [])}
        rows = []
        for k in req_c:
            rc = mean(req_c[k]); rm_ = mean(req_m.get(k, [])) if req_m else 0.0
            uc = pct(util_c.get(k, []), 0.95); um = max(util_m.get(k, []) or [0.0])  # cpu p95 (throttling is survivable), memory peak (OOM is not)
            used_c, used_m = rc * uc, rm_ * um
            rec_c, rec_m = max(0.05, used_c * headroom), max(64 * 1024 ** 2, used_m * headroom)
            hours = len(req_c[k])  # hourly alignment: number of hours the workload was present
            vc, gb = price["default"]["vcpu_hour"], price["default"]["gb_hour"]
            waste_month = ((max(0.0, rc - rec_c) * vc + max(0.0, rm_ - rec_m) / GIB * gb) * hours) / max(1, meta.get("days", 14)) * 30
            rows.append({"namespace": k[0], "workload": k[1], "container": k[2], "req_cpu": rc, "req_mem_gib": rm_ / GIB,
                         "p95_cpu_util": uc, "p95_mem_util": um, "rec_cpu": rec_c, "rec_mem_gib": rec_m / GIB,
                         "hours_present": hours, "unused_cost_month": waste_month, "build": k[0] in build_ns})
        rows.sort(key=lambda r: -r["unused_cost_month"])
        usage["workloads"] = rows
        usage["unused_cost_month"] = sum(r["unused_cost_month"] for r in rows)
        usage["build_unused_cost_month"] = sum(r["unused_cost_month"] for r in rows if r["build"])
        # proposed tiers from build containers' recommended requests
        build_rows = [r for r in rows if r["build"]] or rows
        if build_rows:
            rc_ = sorted(r["rec_cpu"] for r in build_rows); rm2 = sorted(r["rec_mem_gib"] for r in build_rows)
            def tier(q):
                return {"cpu": round(pct(rc_, q) * 2) / 2 or 0.5, "memory_gib": max(0.5, round(pct(rm2, q) * 2) / 2)}
            small, medium, large = tier(0.5), tier(0.9), tier(1.0)
            if medium == large:  # few samples: keep tiers distinct by giving "large" headroom
                large = {"cpu": medium["cpu"] * 2, "memory_gib": medium["memory_gib"] * 2}
            rep["tiers"] = {"small": small, "medium": medium, "large": large,
                            "_derived_from": f"{len(build_rows)} build container(s), p95 usage x headroom {headroom}" + ("; fewer than 10 samples, treat as a starting point" if len(build_rows) < 10 else "")}
    node_req, node_alloc = load_ts("node_req_cores"), load_ts("node_alloc_cores")
    if node_req and node_alloc:
        by_node_req = {t["resource"]["labels"].get("node_name"): series_points(t) for t in node_req}
        by_node_alloc = {t["resource"]["labels"].get("node_name"): series_points(t) for t in node_alloc}
        total_alloc = sum(sum(v) for v in by_node_alloc.values())
        total_req = sum(sum(v) for v in by_node_req.values())
        unalloc_core_hours = max(0.0, total_alloc - total_req)
        days = max(1, meta.get("days", 14))
        usage["unallocated_core_hours"] = unalloc_core_hours
        usage["unallocated_pct"] = (unalloc_core_hours / total_alloc * 100) if total_alloc else 0
        usage["unallocated_cost_month"] = unalloc_core_hours / days * 30 * price["default"]["vcpu_hour"]
    rep["usage"] = usage
    if usage.get("unallocated_pct", 0) > 30:
        rep["recommendations"].append({"area": "cluster", "priority": "high", "item": f"{usage['unallocated_pct']:.0f}% of allocatable CPU over the window was never requested by any pod (unallocated): tune node pool min sizes, machine shapes and scale-down, before touching manifests"})
    if usage.get("workloads"):
        top = [r for r in usage["workloads"][:5] if r["unused_cost_month"] > 1]
        for r in top:
            rep["recommendations"].append({"area": "manifests", "priority": "high" if r["unused_cost_month"] > 50 else "medium",
                                           "item": f"{r['namespace']}/{r['workload']}[{r['container']}] requests {r['req_cpu']:.2g} cpu / {r['req_mem_gib']:.1f} GiB but p95 cpu / peak memory usage is {r['p95_cpu_util'] * 100:.0f}% / {r['p95_mem_util'] * 100:.0f}%; recommend {r['rec_cpu']:.2g} cpu / {r['rec_mem_gib']:.1f} GiB (~${r['unused_cost_month']:.0f}/month)"})

    # ---- autoscaler logs ------------------------------------------------------------------------------
    logs = load_json(d, "autoscaler_logs.json")
    if logs:
        reasons = defaultdict(int); scale_down = 0; scale_up = 0
        for e in logs if isinstance(logs, list) else []:
            jp = e.get("jsonPayload", {})
            if "noDecisionStatus" in jp:
                for n in (jp["noDecisionStatus"].get("noScaleDown", {}).get("nodes") or []):
                    reasons[(n.get("reason") or {}).get("messageId", "unknown")] += 1
            if "decision" in jp:
                if "scaleDown" in jp["decision"]:
                    scale_down += 1
                if "scaleUp" in jp["decision"]:
                    scale_up += 1
        rep["autoscaler"] = {"scale_up_decisions": scale_up, "scale_down_decisions": scale_down, "no_scale_down_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1]))}
        if reasons:
            top_reason = next(iter(rep["autoscaler"]["no_scale_down_reasons"]))
            rep["recommendations"].append({"area": "cluster", "priority": "medium", "item": f"most frequent reason nodes were not scaled down: {top_reason} ({reasons[top_reason]} occurrences); see reference/autoscaler-reasons.md"})

    # ---- TeamCity ---------------------------------------------------------------------------------------
    tb = load_json(d, "teamcity_builds.json")
    if tb:
        per_type = defaultdict(lambda: {"count": 0, "duration_h": 0.0, "queue_h": 0.0})
        hourly = defaultdict(int)
        for b in tb.get("build", []):
            t = per_type[b.get("buildTypeId", "?")]
            t["count"] += 1
            try:
                st = datetime.strptime(b["startDate"], "%Y%m%dT%H%M%S%z"); fi = datetime.strptime(b["finishDate"], "%Y%m%dT%H%M%S%z"); qu = datetime.strptime(b["queuedDate"], "%Y%m%dT%H%M%S%z")
                t["duration_h"] += (fi - st).total_seconds() / 3600; t["queue_h"] += (st - qu).total_seconds() / 3600
                cur = st.replace(minute=0, second=0, microsecond=0)
                while cur < fi:
                    hourly[cur] += 1; cur = cur.replace(hour=cur.hour) + (fi - fi)  # placeholder increment below
                    cur = datetime.fromtimestamp(cur.timestamp() + 3600, tz=cur.tzinfo)
            except (KeyError, ValueError):
                pass
        days = max(1, meta.get("days", 14))
        types = sorted(({"buildType": k, "per_day": v["count"] / days, "avg_duration_min": v["duration_h"] / v["count"] * 60 if v["count"] else 0,
                         "avg_queue_min": v["queue_h"] / v["count"] * 60 if v["count"] else 0} for k, v in per_type.items()), key=lambda x: -x["per_day"])
        rep["teamcity"] = {"builds": sum(v["count"] for v in per_type.values()), "types": types[:20],
                           "peak_concurrency": max(hourly.values()) if hourly else 0, "mean_concurrency": mean(list(hourly.values())) if hourly else 0}
        bt = load_json(d, "teamcity_buildtypes.json")
        if bt:
            k8s_types = []
            for t in bt.get("buildType", []):
                blob = json.dumps(t)
                if re.search(r"kubectl|helm|replicas|kubernetes", blob, re.I):
                    k8s_types.append(t.get("id"))
            rep["teamcity"]["k8s_related_build_types"] = k8s_types[:50]
        if rep["teamcity"]["peak_concurrency"]:
            rep["recommendations"].append({"area": "demand", "priority": "info", "item": f"TeamCity ran {rep['teamcity']['builds']} builds in {days} days; peak {rep['teamcity']['peak_concurrency']} concurrent, mean {rep['teamcity']['mean_concurrency']:.1f}: size the build node pool max for the peak and min for zero"})

    # ---- namespaces ranked by cost, with per-namespace mitigation levers ---------------------------------
    ns_rows = {}
    vc, gb = price["default"]["vcpu_hour"], price["default"]["gb_hour"]
    hpm = price["hours_per_month"]
    for ns, info in rep["snapshot"].get("namespaces", {}).items():
        ns_rows[ns] = {"namespace": ns, "req_cpu_now": info["req_cpu"], "req_mem_gib_now": info["req_mem"] / GIB, "pods": info["pods"],
                       "no_requests": info["no_requests"], "completed": info["completed"],
                       "cost_month_now": (info["req_cpu"] * vc + info["req_mem"] / GIB * gb) * hpm,
                       "unused_cost_month": 0.0, "req_core_hours": 0.0, "p95_cpu_util": None, "top_workload": None, "build": ns in build_ns, "billed_month": None}
    for r in usage.get("workloads", []):
        row = ns_rows.setdefault(r["namespace"], {"namespace": r["namespace"], "req_cpu_now": 0.0, "req_mem_gib_now": 0.0, "pods": 0, "no_requests": 0, "completed": 0,
                                                  "cost_month_now": 0.0, "unused_cost_month": 0.0, "req_core_hours": 0.0, "p95_cpu_util": None, "top_workload": None, "build": r["namespace"] in build_ns, "billed_month": None})
        row["unused_cost_month"] += r["unused_cost_month"]
        row["req_core_hours"] += r["req_cpu"] * r["hours_present"]
        if row["top_workload"] is None or r["unused_cost_month"] > row["top_workload"][1]:
            row["top_workload"] = (f"{r['workload']}[{r['container']}]", r["unused_cost_month"], r["req_cpu"], r["rec_cpu"], r["req_mem_gib"], r["rec_mem_gib"])
    for ns, row in ns_rows.items():
        rows_ns = [r for r in usage.get("workloads", []) if r["namespace"] == ns and r["req_cpu"] > 0]
        if rows_ns:
            tot = sum(r["req_cpu"] * r["hours_present"] for r in rows_ns)
            row["p95_cpu_util"] = sum(r["p95_cpu_util"] * r["req_cpu"] * r["hours_present"] for r in rows_ns) / tot if tot else None
            days = max(1, meta.get("days", 14))
            row["cost_month_window"] = (row["req_core_hours"] * vc) / days * 30  # cpu only; memory added via now-snapshot ratio
    billing = load_billing(d)
    for ns, cost in billing.items():
        ns_rows.setdefault(ns, {"namespace": ns, "req_cpu_now": 0.0, "req_mem_gib_now": 0.0, "pods": 0, "no_requests": 0, "completed": 0, "cost_month_now": 0.0,
                                "unused_cost_month": 0.0, "req_core_hours": 0.0, "p95_cpu_util": None, "top_workload": None, "build": ns in build_ns, "billed_month": None})["billed_month"] = cost
    stale_by_ns = defaultdict(int)
    for item in rep["snapshot"].get("test_environments_up", []) + rep["snapshot"].get("long_running_build_pods", []):
        stale_by_ns[item.split("/")[0]] += 1
    ranked = sorted(ns_rows.values(), key=lambda r: -(r["billed_month"] if r["billed_month"] is not None else max(r["cost_month_now"], r.get("cost_month_window", 0))))
    total_cost = sum((r["billed_month"] if r["billed_month"] is not None else r["cost_month_now"]) for r in ranked) or 1
    for r in ranked:
        r["share_pct"] = (r["billed_month"] if r["billed_month"] is not None else r["cost_month_now"]) / total_cost * 100
        levers = []
        if r["top_workload"] and r["top_workload"][1] > 1:
            tw = r["top_workload"]
            levers.append(f"right-size {tw[0]}: {tw[2]:.2g} cpu / {tw[4]:.1f} GiB -> {tw[3]:.2g} cpu / {tw[5]:.1f} GiB (~${tw[1]:.0f}/month)")
        if r["p95_cpu_util"] is not None and r["p95_cpu_util"] < 0.4:
            levers.append(f"requests are {r['p95_cpu_util'] * 100:.0f}% used at p95: apply the discovery recommendations across the namespace, then tighten the LimitRange")
        if stale_by_ns.get(r["namespace"]):
            levers.append(f"{stale_by_ns[r['namespace']]} stale environment(s)/long-running pod(s): kube-janitor TTL rule + teardown step in the build")
        if r["no_requests"]:
            levers.append(f"{r['no_requests']} pod(s) without requests: LimitRange defaults")
        if r["completed"]:
            levers.append(f"{r['completed']} completed pod(s) lingering: ttlSecondsAfterFinished")
        if r["build"]:
            levers.append("build namespace: schedule on the spot pool; quota sized to peak concurrency")
        if not levers:
            levers.append("no obvious lever from this data; check the billing export by label and HPA/VPA settings")
        r["levers"] = levers
    rep["namespaces"] = ranked
    if billing:
        rep["billing_source"] = "billing export (billing.csv)"

    prio = {"high": 0, "medium": 1, "low": 2, "info": 3}
    rep["recommendations"].sort(key=lambda r: prio.get(r["priority"], 9))
    return rep


# ----------------------------------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------------------------------
def render(rep):
    L = ["## GKE cost-efficiency discovery report", ""]
    c = rep["cluster"]
    if c:
        L += [f"Cluster **{c['name']}** ({c['location']}), {'Autopilot' if c['autopilot'] else 'Standard'}, {c['current_nodes']} node(s), autoscaling profile {c['autoscaling_profile']}, VPA {'on' if c['vpa'] else 'off'}", "",
              "| node pool | machine | spot | autoscaling | min/zone | max/zone | zones |", "|---|---|---|---|---|---|---|"]
        for p in c["node_pools"]:
            L.append(f"| {p['name']} | {p['machine_type']} | {'yes' if p['spot'] else 'no'} | {'yes' if p['autoscaling'] else 'no'} | {p['min']} | {p['max']} | {p['locations']} |")
        L.append("")
    u = rep["usage"]
    s = rep["snapshot"]
    L += ["### Where the waste is", ""]
    if u.get("unused_cost_month") is not None:
        L.append(f"- **Unused (requested but not consumed, p95):** ~${u['unused_cost_month']:.0f}/month across {len(u['workloads'])} workload container(s); build namespaces ~${u.get('build_unused_cost_month', 0):.0f}/month")
    if u.get("unallocated_pct") is not None:
        L.append(f"- **Unallocated (nodes idle, never requested):** {u['unallocated_pct']:.0f}% of allocatable CPU over the window, ~${u['unallocated_cost_month']:.0f}/month")
    if s.get("unallocated_now"):
        un = s["unallocated_now"]
        L.append(f"- **Unallocated right now:** {un['cpu']:.1f} of {un['alloc_cpu']:.1f} cores and {un['mem'] / GIB:.0f} of {un['alloc_mem'] / GIB:.0f} GiB not requested (~${un['cost_month']:.0f}/month at current node count)")
    if not (u.get("unused_cost_month") is not None or u.get("unallocated_pct") is not None or s.get("unallocated_now")):
        L.append("- no usage data collected yet (see Missing inputs)")
    L.append("")
    if rep.get("namespaces"):
        src = rep.get("billing_source", "list-price estimate from requests")
        L += [f"### Namespaces by cost ({src})", "", "| namespace | ~$/month | share | requests now | p95 cpu use | unused ~$/month | pods | mitigation |", "|---|---|---|---|---|---|---|---|"]
        for r in rep["namespaces"][:12]:
            cost = r["billed_month"] if r["billed_month"] is not None else r["cost_month_now"]
            util = f"{r['p95_cpu_util'] * 100:.0f}%" if r["p95_cpu_util"] is not None else "-"
            L.append(f"| {r['namespace']}{' (build)' if r['build'] else ''} | {cost:.0f} | {r['share_pct']:.0f}% | {r['req_cpu_now']:.1f} cpu / {r['req_mem_gib_now']:.0f} GiB | {util} | {r['unused_cost_month']:.0f} | {r['pods']} | {'; '.join(r['levers'][:3])} |")
        L.append("")
    if u.get("workloads"):
        L += ["### Top over-requested workloads (p95 usage vs requests)", "", "| namespace/workload[container] | requests | p95 cpu / peak mem | recommended | ~$/month wasted |", "|---|---|---|---|---|"]
        for r in u["workloads"][:15]:
            L.append(f"| {r['namespace']}/{r['workload']}[{r['container']}]{' (build)' if r['build'] else ''} | {r['req_cpu']:.2g} cpu / {r['req_mem_gib']:.1f} GiB | {r['p95_cpu_util'] * 100:.0f}% / {r['p95_mem_util'] * 100:.0f}% | {r['rec_cpu']:.2g} cpu / {r['rec_mem_gib']:.1f} GiB | {r['unused_cost_month']:.0f} |")
        L.append("")
    if s.get("unallocated_now"):
        L += ["### Nodes with the most idle capacity (now)", "", "| node | pool | type | spot | pods | cpu requested | mem requested | ~$/month idle |", "|---|---|---|---|---|---|---|---|"]
        for n in s["unallocated_now"]["nodes"][:10]:
            L.append(f"| {n['node']} | {n['pool']} | {n['type']} | {'yes' if n['spot'] else 'no'} | {n['pods']} | {n['cpu_req_pct']:.0f}% | {n['mem_req_pct']:.0f}% | {n['unalloc_cost_month']:.0f} |")
        L.append("")
    for title, key in (("Pods without requests", "no_requests"), ("Limit/request ratio >= 4x", "ratio_issues"), ("Build pods running longer than expected", "long_running_build_pods"),
                       ("Test environments left up", "test_environments_up"), ("Jobs without ttlSecondsAfterFinished", "jobs_without_ttl"), ("Scale-down blockers", "scale_down_blockers")):
        if s.get(key):
            L += [f"### {title} ({len(s[key])} shown)", ""] + [f"- {x}" for x in s[key][:15]] + [""]
    if s.get("lingering_completed_pods"):
        L += [f"Completed/failed pods still present: {s['lingering_completed_pods']}", ""]
    kj = s.get("kube_janitor")
    if kj is not None and (kj.get("deployed") or kj.get("unannotated_in_build_namespaces") is not None):
        L += ["### kube-janitor", "", f"deployed: {'yes' if kj['deployed'] else 'no'}" + (f"; dry-run: {'YES (deletes nothing)' if kj['dry_run'] else 'no'}; include-namespaces: {kj['include_namespaces'] or 'all'}; exclude-namespaces: {kj['exclude_namespaces'] or '-'}; include-resources: {kj['include_resources'] or 'all'}; rules-file: {kj['rules_file'] or 'none'}; interval: {kj['interval'] or 'default'}" if kj["deployed"] else "")]
        un = kj.get("unannotated_in_build_namespaces") or []
        if un:
            L.append(f"build-namespace workloads without janitor/ttl or janitor/expires: {len(un)} (e.g. {', '.join(un[:5])})")
        L.append("")
    a = rep["autoscaler"]
    if a:
        L += ["### Cluster autoscaler", "", f"scale-up decisions {a['scale_up_decisions']}, scale-down decisions {a['scale_down_decisions']}"]
        for k, v in list(a["no_scale_down_reasons"].items())[:8]:
            L.append(f"- not scaled down: {k} x{v}")
        L.append("")
    t = rep["teamcity"]
    if t:
        L += ["### TeamCity demand", "", f"{t['builds']} builds; peak concurrency {t['peak_concurrency']}, mean {t['mean_concurrency']:.1f}", "", "| build type | builds/day | avg duration (min) | avg queue (min) |", "|---|---|---|---|"]
        for x in t["types"][:10]:
            L.append(f"| {x['buildType']} | {x['per_day']:.1f} | {x['avg_duration_min']:.0f} | {x['avg_queue_min']:.0f} |")
        if t.get("k8s_related_build_types"):
            L.append(f"\nBuild configurations touching Kubernetes (kubectl/helm/replicas): {', '.join(t['k8s_related_build_types'][:15])}")
        L.append("")
    if rep["tiers"]:
        L += ["### Proposed build size tiers (requests)", "", "```json", json.dumps({k: v for k, v in rep["tiers"].items() if not k.startswith("_")}, indent=1), "```", f"{rep['tiers']['_derived_from']}", ""]
    L += ["### Recommendations", ""]
    for r in rep["recommendations"] or [{"priority": "info", "area": "-", "item": "nothing to recommend yet; provide the missing inputs"}]:
        L.append(f"- [{r['priority']}] ({r['area']}) {r['item']}")
    if rep["missing"]:
        L += ["", "### Missing inputs (provide these and re-run collect.sh)", ""] + [f"- {m['input']}: {m['reason']}" for m in rep["missing"]]
    L += ["", "Cost figures are approximations from list prices for ranking only; the billing export is the authority."]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir")
    ap.add_argument("--build-namespaces", default="")
    ap.add_argument("--headroom", type=float, default=1.25, help="multiplier on p95 usage for recommended requests")
    ap.add_argument("--idle-hours", type=float, default=4, help="build pod / test env older than this is suspicious")
    ap.add_argument("--pricing", default=os.path.join(os.path.dirname(HERE), "reference", "pricing.json"))
    ap.add_argument("--tiers-out", help="write proposed build tiers JSON here")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    pricing = json.load(open(a.pricing))
    rep = analyze(a.dir, [x for x in a.build_namespaces.split(",") if x], a.headroom, pricing, a.idle_hours)
    if a.tiers_out and rep["tiers"]:
        with open(a.tiers_out, "w") as f:
            json.dump({"tiers": {k: v for k, v in rep["tiers"].items() if not k.startswith("_")}, "source": rep["tiers"]["_derived_from"]}, f, indent=1)
    if a.json:
        print(json.dumps(rep, indent=1, default=str))
    else:
        print(render(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
