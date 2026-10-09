---
name: k8s-rightsize
description: >
  Use to right-size requests and limits of build and test workloads in Kubernetes manifests from
  measured usage (a gke-cost-discovery report), with a diff and evidence per container.
allowed-tools: Bash(*/k8s-rightsize/scripts/run.sh *), Bash(python3 */k8s-rightsize/scripts/rightsize.py *), Bash(*/k8s-rightsize/scripts/rightsize.py *), Bash(python3 */gke-cost-discovery/scripts/analyze.py *), Bash(kubectl get *), Read, Write, Edit, Glob
---

# k8s-rightsize: from evidence to an edited manifest

Evidence comes from one of three places, in order of preference:

1. **Published discovery report** (no credentials needed by the developer). The platform team
   runs `gke-cost-discovery` on a schedule and commits `discovery/latest/report.json` and
   `build-tiers.json` to the manifests repo (or sets `GKE_DISCOVERY_REPORT`); the script finds
   them automatically.
2. **Your own discovery run** if you have read-only cluster and monitoring access.
3. **A local measurement** on minikube/kind with metrics-server (`scripts/measure.py`): run the
   build or test job there, sample its usage for the whole run, and use the resulting
   `report.json`. Sizing from a short local run is only as good as the run: exercise the mock
   servers with the real test suite, not an idle deployment.

CPU is sized from p95 (throttling is survivable), memory from the observed peak (exceeding it is
an OOM kill); both get the headroom factor.

Run the scripts by their installed path from the working directory (for example
`.claude/skills/k8s-rightsize/scripts/...` or `.agents/skills/k8s-rightsize/scripts/...`), one
command per call: no `cd` into a skill folder, no shell variable, nothing piped or chained after it.
Permission rules match the literal command; Antigravity denies a chain if any part lacks a grant,
and installed skill folders are links outside the working directory.

```bash
# 1a. platform: produce and publish the evidence (gke-cost-discovery)
../gke-cost-discovery/scripts/analyze.py ./discovery/latest --json > discovery/latest/report.json
../gke-cost-discovery/scripts/analyze.py ./discovery/latest --tiers-out build-tiers.json
# 1b. developer without access: measure locally while the job runs (read-only kubectl top sampling)
scripts/measure.py --namespace test-envs --context minikube --duration 300 --interval 10 --out report.json

# 2. get the namespace's manifests: from the repo / TeamCity build template, or a live dump
kubectl get jobs,deployments,cronjobs -n teamcity-agents -o yaml > teamcity-agents.yaml   # read-only

# 3. propose, review, apply
scripts/run.sh teamcity-agents.yaml --report report.json --tiers build-tiers.json --lifecycle          # diff
scripts/run.sh teamcity-agents.yaml --report report.json --tiers build-tiers.json --lifecycle --write  # apply to the file
../k8s-manifest-review/scripts/run.sh teamcity-agents.yaml --build                                          # must pass
```

Options: `--headroom 1.25` (multiplier on p95), `--cpu-limit-factor 2` (cpu limit = request x
factor; memory limit always equals the request), `--policy k8s-review.json` (tier label,
annotation, TTL values), `--json`.

`scripts/run.sh` installs PyYAML into the venv shared by the skills (`~/.cache/astgraph/venv`) on
first use; `scripts/rightsize.py` still works directly with any Python that has PyYAML.

## What it does, precisely

| situation | result |
|---|---|
| usage row found for namespace/workload/container | cpu request = p95 x headroom, memory request = peak x headroom, rounded up to 50m / 64Mi; limits = memory request and cpu x factor |
| no usage row, requests above the workload's tier | requests lowered to the tier cap |
| no usage row, no requests at all | small tier |
| no usage row, requests within tier | untouched (nothing is raised without evidence) |
| `--lifecycle` | Jobs: `ttlSecondsAfterFinished`, `activeDeadlineSeconds`, `backoffLimit: 1` when missing; CronJob template TTL; Deployments/StatefulSets: `janitor/ttl` when neither `janitor/ttl` nor `janitor/expires` is set |

Workload names are matched after stripping generated suffixes (`tc-agent-unit-8814`,
`api-pr-231-7d9f`) and then by shorter prefixes, so per-build Jobs and per-PR environments
match the monitoring's controller names. A second run on the result reports "no changes".

## Procedure for the agent

1. Never right-size without the report unless the developer accepts tier capping only; say
   which basis each change used (`usage` or `tier`).
2. Show the diff and the evidence table first. Explain each reduction in one line: old,
   new, p95, headroom.
3. Apply with `--write` only to files in a repository or a scratch copy, never to live objects
   (`kubectl apply` is not this skill's job; GitOps or the developer's build template is).
4. Re-run `k8s-manifest-review`; the result must pass the gate. Report requests freed per pod
   and, multiplied by the TeamCity peak concurrency from the discovery report, the capacity
   freed at peak.
5. Note limits: PyYAML re-serialises the file (comments and formatting are lost, keys keep
   their order); Helm templates must be right-sized through their values files instead
   (`helm-chart-review` shows the effective values per environment).
