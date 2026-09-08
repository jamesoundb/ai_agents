#!/usr/bin/env python3
"""
rightsize.py — rewrite requests/limits of build and test workloads from measured usage.

  rightsize.py MANIFEST... --report discovery.json [--tiers build-tiers.json] [--policy k8s-review.json]
               [--headroom 1.25] [--cpu-limit-factor 2] [--lifecycle] [--write] [--json]

  --report   JSON from `analyze.py DIR --json` (gke-cost-discovery); its usage.workloads rows carry
             the p95 utilization and recommended requests per namespace/workload/container.
  Matching   namespace + workload name + container name; the workload name is compared after
             stripping generated suffixes (-<hash>, -<number>, -pr-123) so TeamCity/PR jobs match.
  Fallback   no usage row: requests above the workload's tier are lowered to the tier; missing
             requests get the small tier. Nothing is raised without evidence.
  Limits     memory limit = memory request; cpu limit = request x --cpu-limit-factor.
  --lifecycle adds ttlSecondsAfterFinished/activeDeadlineSeconds/backoffLimit to Jobs and the
             janitor/ttl annotation to test Deployments when missing.
Output: unified diff per file (or --write to overwrite) plus a per-container evidence table.
PyYAML re-serialises the documents, so comments are dropped; review the diff before committing.
"""
import argparse
import difflib
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from yamlload import load_all  # noqa: E402

try:
    import yaml
except ImportError:
    sys.exit("rightsize needs PyYAML to re-serialise manifests: pip install pyyaml")

GIB = 1024 ** 3
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Pod", "ReplicaSet"}
SUFFIX_RE = re.compile(r"(-\d+|-[0-9a-f]{6,10}|-[a-z0-9]{5})+$")  # generated suffixes: numbers, hashes, pod-template hashes
DEFAULTS = {"tiers": {"small": {"cpu": 1, "memory_gib": 2}, "medium": {"cpu": 2, "memory_gib": 4}, "large": {"cpu": 4, "memory_gib": 8}},
            "default_tier": "medium", "tier_label": "build.company.io/tier", "ttl_annotation": "janitor/ttl",
            "job_ttl_seconds": 600, "job_deadline_seconds": 7200, "env_ttl": "8h"}


def cpu(q):
    if q is None:
        return None
    q = str(q)
    return float(q[:-1]) / 1000 if q.endswith("m") else float(q)


def mem(q):
    if q is None:
        return None
    q = str(q)
    for u, mult in {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4, "K": 1e3, "M": 1e6, "G": 1e9}.items():
        if q.endswith(u):
            return float(q[:-len(u)]) * mult
    return float(q)


def fmt_cpu(v):
    m = int(math.ceil(v * 1000 / 50.0) * 50)  # 50m steps
    return f"{m}m" if m < 1000 or m % 1000 else str(m // 1000)


def fmt_mem(b):
    mi = int(math.ceil(b / (64 * 1024 ** 2)) * 64)  # 64Mi steps
    return f"{mi // 1024}Gi" if mi >= 1024 and mi % 1024 == 0 else f"{mi}Mi"


def norm_name(n):
    return SUFFIX_RE.sub("", n or "")


def pod_spec(doc):
    kind, spec = doc.get("kind"), doc.get("spec") or {}
    if kind == "Pod":
        return spec
    if kind == "CronJob":
        return (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec")
    if kind in WORKLOAD_KINDS:
        return (spec.get("template") or {}).get("spec")
    return None


def name_candidates(name):
    """api-pr-231-7d9f -> [api-pr-231-7d9f, api-pr, api] : exact, suffix-stripped, then shorter prefixes."""
    out = [name]
    base = norm_name(name)
    if base != name:
        out.append(base)
    parts = base.split("-")
    while len(parts) > 1:
        parts = parts[:-1]
        out.append("-".join(parts))
    return list(dict.fromkeys(out))


def lookup(idx, ns, name, cname):
    for cand in name_candidates(name):
        row = idx.get((ns, cand, cname))
        if row:
            return row
    for cand in name_candidates(name):  # single-container workloads whose container name differs
        cands = [v for k, v in idx.items() if k[0] == ns and k[1] == cand]
        if len(cands) == 1:
            return cands[0]
    return None


def build_index(report):
    idx = {}
    for r in (report.get("usage") or {}).get("workloads", []):
        idx[(r["namespace"], norm_name(r["workload"]), r["container"])] = r
    return idx


def rightsize_doc(doc, idx, P, headroom, cpu_factor, lifecycle):
    """Mutate doc in place; return list of change records."""
    changes = []
    if not isinstance(doc, dict) or doc.get("kind") not in WORKLOAD_KINDS:
        return changes
    meta = doc.get("metadata") or {}
    ns, name, kind = meta.get("namespace", ""), meta.get("name", ""), doc["kind"]
    labels = meta.get("labels") or {}
    tier_name = labels.get(P["tier_label"]) or P["default_tier"]
    tier = P["tiers"].get(tier_name) or P["tiers"][P["default_tier"]]
    small = P["tiers"]["small"]
    ps = pod_spec(doc)
    if ps is None:
        return changes
    for c in ps.get("containers") or []:
        cname = c.get("name", "")
        res = c.setdefault("resources", {})
        req, lim = res.get("requests") or {}, res.get("limits") or {}
        old = {"cpu": cpu(req.get("cpu")), "mem": mem(req.get("memory")), "lcpu": cpu(lim.get("cpu")), "lmem": mem(lim.get("memory"))}
        row = lookup(idx, ns, name, cname)
        if row:
            # recompute from p95 x headroom (the report used its own headroom; keep the caller's)
            new_cpu = max(0.05, row["req_cpu"] * row["p95_cpu_util"] * headroom)
            new_mem = max(64 * 1024 ** 2, row["req_mem_gib"] * GIB * row["p95_mem_util"] * headroom)
            evidence = f"p95 cpu {row['p95_cpu_util'] * 100:.0f}% / peak mem {row['p95_mem_util'] * 100:.0f}% of {row['req_cpu']:.2g} cpu / {row['req_mem_gib']:.1f} GiB over {row['hours_present']}h, x{headroom}"
            if row["p95_cpu_util"] >= 0.95 and old["lcpu"] and old["cpu"] and old["lcpu"] <= old["cpu"] * 1.05:
                evidence += "; CPU-BOUND AT ITS LIMIT: measured usage is capped by the old limit, true demand may be higher (raise the limit, re-measure)"
            if row["p95_mem_util"] >= 0.9:
                evidence += "; memory peak near the old request: keep headroom, watch for OOMKilled"
            basis = "usage"
        else:
            cap_cpu, cap_mem = tier["cpu"], tier["memory_gib"] * GIB
            new_cpu = min(old["cpu"], cap_cpu) if old["cpu"] else small["cpu"]
            new_mem = min(old["mem"], cap_mem) if old["mem"] else small["memory_gib"] * GIB
            evidence = f"no usage data: {'capped at' if old['cpu'] else 'defaulted to'} tier {tier_name if old['cpu'] else 'small'}"
            basis = "tier"
        # never raise without usage evidence; with evidence, allow raising only if p95 exceeded the old request
        if basis == "tier" and old["cpu"] and new_cpu >= old["cpu"] and old["mem"] and new_mem >= old["mem"]:
            continue
        new_req = {"cpu": fmt_cpu(new_cpu), "memory": fmt_mem(new_mem)}
        if "ephemeral-storage" in req:
            new_req["ephemeral-storage"] = req["ephemeral-storage"]
        new_lim = {"cpu": fmt_cpu(new_cpu * cpu_factor), "memory": new_req["memory"]}
        if "ephemeral-storage" in lim:
            new_lim["ephemeral-storage"] = lim["ephemeral-storage"]
        if new_req == req and new_lim == lim:
            continue
        res["requests"], res["limits"] = new_req, new_lim
        changes.append({"object": f"{kind}/{name}", "namespace": ns, "container": cname,
                        "old": f"{req.get('cpu', '-')} / {req.get('memory', '-')} (lim {lim.get('cpu', '-')} / {lim.get('memory', '-')})",
                        "new": f"{new_req['cpu']} / {new_req['memory']} (lim {new_lim['cpu']} / {new_lim['memory']})",
                        "basis": basis, "evidence": evidence,
                        "saving_cpu": (old["cpu"] or 0) - new_cpu, "saving_mem_gib": ((old["mem"] or 0) - new_mem) / GIB})
    if lifecycle:
        spec = doc.setdefault("spec", {})
        if kind == "Job":
            for k, v in (("ttlSecondsAfterFinished", P["job_ttl_seconds"]), ("activeDeadlineSeconds", P["job_deadline_seconds"]), ("backoffLimit", 1)):
                if k not in spec:
                    spec[k] = v
                    changes.append({"object": f"{kind}/{name}", "namespace": ns, "container": "-", "old": f"{k} unset", "new": f"{k}: {v}", "basis": "lifecycle", "evidence": "policy", "saving_cpu": 0, "saving_mem_gib": 0})
        if kind == "CronJob":
            js = spec.setdefault("jobTemplate", {}).setdefault("spec", {})
            if "ttlSecondsAfterFinished" not in js:
                js["ttlSecondsAfterFinished"] = P["job_ttl_seconds"]
                changes.append({"object": f"{kind}/{name}", "namespace": ns, "container": "-", "old": "ttlSecondsAfterFinished unset", "new": f"ttlSecondsAfterFinished: {P['job_ttl_seconds']}", "basis": "lifecycle", "evidence": "policy", "saving_cpu": 0, "saving_mem_gib": 0})
        if kind in ("Deployment", "StatefulSet"):
            ann = meta.setdefault("annotations", {})
            if P["ttl_annotation"] not in ann and "janitor/expires" not in ann:
                ann[P["ttl_annotation"]] = P["env_ttl"]
                changes.append({"object": f"{kind}/{name}", "namespace": ns, "container": "-", "old": "no TTL annotation", "new": f"{P['ttl_annotation']}: {P['env_ttl']}", "basis": "lifecycle", "evidence": "kube-janitor", "saving_cpu": 0, "saving_mem_gib": 0})
    return changes


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifests", nargs="+")
    ap.add_argument("--report", help="analyze.py --json output (default: $GKE_DISCOVERY_REPORT, ./report.json, ./discovery/latest/report.json); without it only tier capping/defaults apply")
    ap.add_argument("--tiers")
    ap.add_argument("--policy")
    ap.add_argument("--headroom", type=float, default=1.25)
    ap.add_argument("--cpu-limit-factor", type=float, default=2.0)
    ap.add_argument("--lifecycle", action="store_true")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    P = json.loads(json.dumps(DEFAULTS))
    for c in ([a.policy] if a.policy else []) + ["k8s-review.json"]:
        if c and os.path.exists(c):
            P.update({k: v for k, v in json.load(open(c)).items() if k in P or k == "tiers"})
            break
    for c in ([a.tiers] if a.tiers else []) + ["build-tiers.json"]:
        if c and os.path.exists(c):
            data = json.load(open(c)); P["tiers"] = data.get("tiers", data)
            break
    if not a.report:
        for cand in [os.environ.get("GKE_DISCOVERY_REPORT"), "report.json", os.path.join("discovery", "latest", "report.json"), os.path.join("discovery", "report.json")]:
            if cand and os.path.exists(cand):
                a.report = cand
                print(f"using report {cand}", file=sys.stderr)
                break
    report = json.load(open(a.report)) if a.report else {}
    idx = build_index(report)
    if not idx and a.report:
        print("warning: the report has no usage.workloads rows (no Cloud Monitoring data); applying tier capping only", file=sys.stderr)
    all_changes, diffs = [], []
    for f in a.manifests:
        text = open(f, encoding="utf-8").read()
        if "{{" in text:
            print(f"skip {f}: Helm template (render first, or change values instead)", file=sys.stderr)
            continue
        docs, _ = load_all(text)
        changes = []
        for d in docs:
            changes += rightsize_doc(d, idx, P, a.headroom, a.cpu_limit_factor, a.lifecycle)
        if not changes:
            continue
        new_text = yaml.safe_dump_all([d for d in docs if d is not None], sort_keys=False, default_flow_style=False)
        diffs.append("".join(difflib.unified_diff(text.splitlines(True), new_text.splitlines(True), fromfile=f, tofile=f + " (rightsized)")))
        if a.write:
            open(f, "w", encoding="utf-8").write(new_text)
        all_changes += [dict(c, file=f) for c in changes]
    if a.json:
        print(json.dumps({"changes": all_changes, "written": a.write}, indent=1))
        return 0
    if not all_changes:
        print("no changes: every container is at or below its evidence/tier, or no workloads found")
        return 0
    print("## Right-sizing " + ("applied" if a.write else "proposal (diff; add --write to apply)"))
    print("\n| file | object | container | old requests (limits) | new requests (limits) | basis | evidence |\n|---|---|---|---|---|---|---|")
    for c in all_changes:
        print(f"| {c['file']} | {c['object']} | {c['container']} | {c['old']} | {c['new']} | {c['basis']} | {c['evidence']} |")
    sc = sum(c["saving_cpu"] for c in all_changes); sm = sum(c["saving_mem_gib"] for c in all_changes)
    print(f"\nRequests freed per pod instance: {sc:.2f} cpu, {sm:.1f} GiB (multiply by concurrent instances for capacity)")
    for d in diffs:
        print("\n```diff\n" + d + "```")
    return 0


if __name__ == "__main__":
    sys.exit(main())
