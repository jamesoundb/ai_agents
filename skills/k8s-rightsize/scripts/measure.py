#!/usr/bin/env python3
"""
measure.py — measure a build/test job's real usage on a lower environment (minikube/kind with
metrics-server, or any cluster you may read) and emit a gke-cost-discovery compatible report, so
rightsize.py can size the manifest without Cloud Monitoring access.

  measure.py --namespace NS [--selector app=x] [--duration 300] [--interval 10] [--context CTX]
             [--out report.json]

Samples `kubectl top pods --containers` every --interval seconds for --duration seconds, reads the
pods' requests and owners once, and writes usage.workloads rows with p95 CPU and PEAK memory as
fractions of requests. Read-only. Run the job first (kubectl apply, or the TeamCity build), then
this script alongside it; sample the whole run including startup.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from collections import defaultdict

GIB = 1024 ** 3


def cpu(q):
    q = str(q)
    return float(q[:-1]) / 1000 if q.endswith("m") else float(q)


def mem(q):
    q = str(q)
    for u, mult in {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4}.items():
        if q.endswith(u):
            return float(q[:-len(u)]) * mult
    return float(q)


def k(args, ctx):
    cmd = ["kubectl", "--request-timeout=20s"] + (["--context", ctx] if ctx else []) + args
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip())
    return p.stdout


def pct(vals, q):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[max(0, min(len(s) - 1, int(round((len(s) - 1) * q))))]


def owner_name(pod):
    refs = pod["metadata"].get("ownerReferences") or []
    if not refs:
        return pod["metadata"]["name"]
    o = refs[0]
    name = o["name"]
    if o["kind"] == "ReplicaSet":  # Deployment -> ReplicaSet -> Pod: strip the pod-template hash
        name = re.sub(r"-[0-9a-f]{8,10}$", "", name)
    return name


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--selector", default="")
    ap.add_argument("--duration", type=int, default=300)
    ap.add_argument("--interval", type=int, default=10)
    ap.add_argument("--context")
    ap.add_argument("--out", default="report.json")
    a = ap.parse_args(argv)
    sel = ["-l", a.selector] if a.selector else []
    samples = defaultdict(lambda: {"cpu": [], "mem": []})
    requests, owners = {}, {}
    t_end = time.time() + a.duration
    n = 0
    print(f"sampling namespace {a.namespace} every {a.interval}s for {a.duration}s ...", file=sys.stderr)
    while time.time() < t_end:
        try:
            pods = json.loads(k(["get", "pods", "-n", a.namespace, "-o", "json"] + sel, a.context))
            for p in pods.get("items", []):
                pname = p["metadata"]["name"]
                owners[pname] = owner_name(p)
                for c in p["spec"].get("containers", []):
                    req = (c.get("resources") or {}).get("requests") or {}
                    requests[(pname, c["name"])] = (cpu(req["cpu"]) if "cpu" in req else None, mem(req["memory"]) if "memory" in req else None)
            top = k(["top", "pods", "-n", a.namespace, "--containers", "--no-headers"] + sel, a.context)
            for line in top.splitlines():
                parts = line.split()
                if len(parts) >= 4:
                    pname, cname, c_cpu, c_mem = parts[0], parts[1], parts[2], parts[3]
                    samples[(pname, cname)]["cpu"].append(cpu(c_cpu))
                    samples[(pname, cname)]["mem"].append(mem(c_mem))
            n += 1
        except RuntimeError as e:
            print(f"  sample skipped: {e[:120] if isinstance(e, str) else str(e)[:120]}", file=sys.stderr)
        time.sleep(a.interval)
    rows = {}
    for (pname, cname), s in samples.items():
        rc, rm = requests.get((pname, cname), (None, None))
        wl = owners.get(pname, pname)
        key = (wl, cname)
        r = rows.setdefault(key, {"namespace": a.namespace, "workload": wl, "container": cname, "req_cpu": rc or 0.0, "req_mem_gib": (rm or 0) / GIB,
                                  "cpu": [], "mem": [], "samples": 0})
        r["cpu"] += s["cpu"]; r["mem"] += s["mem"]; r["samples"] += len(s["cpu"])
    out = []
    for r in rows.values():
        p95c, peakm = pct(r["cpu"], 0.95), max(r["mem"] or [0])
        rc, rm = r["req_cpu"], r["req_mem_gib"] * GIB
        out.append({"namespace": r["namespace"], "workload": r["workload"], "container": r["container"],
                    "req_cpu": rc if rc else round(p95c, 3), "req_mem_gib": r["req_mem_gib"] if rm else peakm / GIB,
                    "p95_cpu_util": (p95c / rc) if rc else 1.0, "p95_mem_util": (peakm / rm) if rm else 1.0,
                    "rec_cpu": p95c * 1.25, "rec_mem_gib": peakm * 1.25 / GIB,
                    "hours_present": round(a.duration / 3600, 3), "unused_cost_month": 0.0, "build": True,
                    "measured": {"samples": r["samples"], "p95_cpu_cores": p95c, "peak_mem_gib": peakm / GIB, "requests_missing": not rc or not rm}})
    report = {"source": "measure.py (kubectl top samples)", "namespace": a.namespace, "samples": n, "usage": {"workloads": out}}
    json.dump(report, open(a.out, "w"), indent=1)
    print(f"wrote {a.out}: {len(out)} container(s), {n} sample round(s)", file=sys.stderr)
    for r in out:
        print(f"  {r['workload']}[{r['container']}]: p95 cpu {r['measured']['p95_cpu_cores']:.3f} cores, peak mem {r['measured']['peak_mem_gib'] * 1024:.0f} Mi"
              + (f"  (requests {r['req_cpu']:g} cpu / {r['req_mem_gib'] * 1024:.0f} Mi -> {r['p95_cpu_util'] * 100:.0f}% / {r['p95_mem_util'] * 100:.0f}%)" if not r["measured"]["requests_missing"] else "  (no requests set)"), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
