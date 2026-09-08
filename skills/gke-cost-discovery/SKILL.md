---
name: gke-cost-discovery
description: >
  Measure where a GKE Standard cluster wastes money on test builds and test environments:
  unused capacity (requested but not consumed) versus unallocated capacity (nodes idle because
  nothing requested them), top over-requested workloads with recommended requests, stale test
  environments, lingering Jobs, scale-down blockers, node pool settings, autoscaler no-scale-down
  reasons, and TeamCity build demand. Use when asked why the cluster costs too much, to right-size
  build manifests, to derive build size tiers, or before tuning node pools.
allowed-tools: Bash(*/gke-cost-discovery/scripts/collect.sh *), Bash(*/gke-cost-discovery/scripts/analyze.py *), Bash(*/gke-cost-discovery/scripts/synth.py *), Bash(python3 */gke-cost-discovery/scripts/*), Bash(gcloud * describe *), Bash(gcloud * list *), Bash(gcloud * get-credentials *), Bash(kubectl get *), Bash(kubectl top *), Read, Write
---

# gke-cost-discovery: measure before you cut

Two read-only scripts. `scripts/collect.sh` gathers evidence into a directory (every source is
optional and a missing one becomes a `MISSING-*` note); `scripts/analyze.py` turns the directory
into a report with a two-bucket waste model, top offenders in cores, GiB and approximate dollars,
proposed build size tiers, and prioritized recommendations. Neither script changes anything.

```bash
# 1. configure (copy gke-cost-discovery.env.example -> gke-cost-discovery.env and fill in)
set -a; . ./gke-cost-discovery.env; set +a
# 2. collect (read-only gcloud/kubectl/Cloud Monitoring/Cloud Logging/TeamCity REST)
scripts/collect.sh ./discovery/latest
# 3. analyze
scripts/analyze.py ./discovery/latest [--build-namespaces a,b] [--headroom 1.25] [--idle-hours 4] \
                   [--tiers-out build-tiers.json] [--json]
# demo / test without any access
scripts/synth.py /tmp/discovery-demo && scripts/analyze.py /tmp/discovery-demo
```

## Waste model (read the report in this order)

1. **Unallocated** = allocatable minus requested, over time and right now. This is the cluster's
   fault, not the manifests': node pool minimums, oversized machine shapes for the pod shapes,
   slow or blocked scale-down (bare pods, `safe-to-evict=false`, PDBs, system pods), on-demand
   pools for burst work. If this bucket dominates, fix node pools before touching manifests.
2. **Unused** = requested minus p95 usage per workload container. This is the manifests' fault:
   requests copied from a generous template. The report lists the top offenders with a
   recommended request (p95 x headroom) and the approximate monthly waste.
3. **Lifecycle waste**: test environments still up after the run, Jobs without
   `ttlSecondsAfterFinished` / `activeDeadlineSeconds`, completed pods lingering, pods with no
   requests at all (unschedulable for the autoscaler's purposes).
4. **Demand** (TeamCity): builds per day, peak and mean concurrency, average duration and queue
   time per build configuration, and which configurations touch Kubernetes. Peak concurrency
   sizes the build pool maximum; the minimum should usually be zero.
5. **Namespaces by cost**: every namespace ranked by monthly cost (list-price estimate from
   requests, or actual dollars when `billing.csv` from the GKE cost-allocation export is present;
   see `reference/metrics.md`), with share, requests now, p95 utilization, unused cost, and the
   mitigation levers that apply to that namespace (right-size the top workload, LimitRange
   defaults, kube-janitor TTL rule, ttlSecondsAfterFinished, spot pool, quota).

## Procedure for the agent

1. Ask for or locate the `.env` values (project, cluster, location, build namespaces, TeamCity
   URL/token). If any are missing, run anyway: the report lists what is missing.
2. Run collect then analyze. Never run anything that writes to the cluster or project.
3. Present the report's "Where the waste is" numbers first, then the top three actions from the
   Recommendations list, each with the evidence line it came from.
4. Write the proposed tiers (`--tiers-out`) next to the build manifests as `build-tiers.json`;
   the manifest-review and guardrail skills consume it.
5. Say clearly that dollar figures are list-price approximations for ranking; the billing export
   is the authority.

## Access needed (all read-only)

Project roles `roles/container.clusterViewer`, `roles/monitoring.viewer`, `roles/logging.viewer`;
cluster RBAC `view` (via `gcloud container clusters get-credentials`); a TeamCity access token
with read scope. Details of every metric and query: `reference/metrics.md`; autoscaler reason
codes: `reference/autoscaler-reasons.md`; pricing overrides: `reference/pricing.json`.
