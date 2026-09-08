#!/usr/bin/env python3
"""
guardrails.py — generate namespace guardrails for build/test namespaces from build size tiers.

  guardrails.py --tiers build-tiers.json --namespaces ns1,ns2 --out ./guardrails
               [--concurrent-pods 20] [--ttl-hours 8] [--janitor] [--tier-label build.company.io/tier]

Per namespace it writes:
  <ns>-limitrange.yaml     defaults = small tier, max per container = large tier, memory limit = request
  <ns>-resourcequota.yaml  requests.cpu/memory = medium tier x concurrent pods, pod/job counts
  <ns>-kube-janitor-rule.yaml  rules-file entry for kube-janitor (the company standard): default TTL for
                           Deployments/StatefulSets/Jobs in the namespace that carry no janitor/ttl
                           or janitor/expires annotation. Merge it into kube-janitor's rules ConfigMap.
  <ns>-janitor.yaml        (with --standalone-janitor) self-contained CronJob + RBAC for clusters
                           WITHOUT kube-janitor; do not run both.
Plain-text YAML output; verify with `kubectl apply --dry-run=client -f`.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(os.path.dirname(HERE), "templates")


def fmt_cpu(v):
    return f"{int(round(v * 1000))}m" if v < 1 or abs(v - round(v)) > 1e-9 else str(int(v))


def fmt_mem(gib):
    return f"{int(round(gib * 1024))}Mi" if gib < 1 or abs(gib - round(gib)) > 1e-9 else f"{int(gib)}Gi"


def render(name, vars_):
    txt = open(os.path.join(TEMPLATES, name), encoding="utf-8").read()
    for k, v in vars_.items():
        txt = txt.replace("{{" + k + "}}", str(v))
    return txt


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tiers", required=True)
    ap.add_argument("--namespaces", required=True)
    ap.add_argument("--out", default="./guardrails")
    ap.add_argument("--concurrent-pods", type=int, default=20, help="expected concurrent build pods per namespace (quota sizing)")
    ap.add_argument("--ttl-hours", type=int, default=8)
    ap.add_argument("--kube-janitor-rule", action="store_true", default=True, help="emit a kube-janitor rules entry giving unannotated objects in the namespace a default TTL (default on)")
    ap.add_argument("--no-kube-janitor-rule", dest="kube_janitor_rule", action="store_false")
    ap.add_argument("--standalone-janitor", dest="janitor", action="store_true", help="emit the self-contained CronJob janitor (only for clusters WITHOUT kube-janitor)")
    ap.add_argument("--tier-label", default="build.company.io/tier")
    ap.add_argument("--janitor-image", default="alpine/k8s:1.30.14", help="image with sh + kubectl (bitnami/kubectl tags are no longer reliably pullable)")
    a = ap.parse_args(argv)
    data = json.load(open(a.tiers))
    tiers = data.get("tiers", data)
    small, medium, large = tiers["small"], tiers["medium"], tiers["large"]
    os.makedirs(a.out, exist_ok=True)
    written = []
    for ns in [n for n in a.namespaces.split(",") if n]:
        v = {"namespace": ns, "tier_label": a.tier_label, "ttl": f"{a.ttl_hours}h", "janitor_image": a.janitor_image,
             "small_cpu": fmt_cpu(small["cpu"]), "small_mem": fmt_mem(small["memory_gib"]),
             "medium_cpu": fmt_cpu(medium["cpu"]), "medium_mem": fmt_mem(medium["memory_gib"]),
             # default cpu limit must respect maxLimitRequestRatio (4) against the default request,
             # otherwise pods without requests are rejected instead of defaulted (found on minikube)
             "default_cpu_limit": fmt_cpu(min(medium["cpu"], small["cpu"] * 4)),
             "large_cpu": fmt_cpu(large["cpu"]), "large_mem": fmt_mem(large["memory_gib"]),
             "quota_cpu": fmt_cpu(medium["cpu"] * a.concurrent_pods), "quota_mem": fmt_mem(medium["memory_gib"] * a.concurrent_pods),
             "quota_pods": a.concurrent_pods * 2, "quota_jobs": a.concurrent_pods * 4,
             "small_tier_json": json.dumps(small), "medium_tier_json": json.dumps(medium), "large_tier_json": json.dumps(large)}
        for tpl, suffix in (("limitrange.yaml", "limitrange"), ("resourcequota.yaml", "resourcequota")) + ((("kube-janitor-rule.yaml", "kube-janitor-rule"),) if a.kube_janitor_rule else ()) + ((("janitor.yaml", "janitor"),) if a.janitor else ()):
            out = os.path.join(a.out, f"{ns}-{suffix}.yaml")
            with open(out, "w", encoding="utf-8") as f:
                f.write(render(tpl, v))
            written.append(out)
    for w in written:
        print("wrote " + w)
    print(f"\ntiers: small {small}, medium {medium}, large {large}; quota = medium x {a.concurrent_pods} pods")
    print("verify: kubeconform -strict " + a.out + "/*.yaml  (offline) or kubectl apply --dry-run=server -f " + a.out + " against a reachable cluster; then hand to GitOps, never apply from this skill")
    return 0


if __name__ == "__main__":
    sys.exit(main())
