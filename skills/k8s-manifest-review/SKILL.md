---
name: k8s-manifest-review
description: >
  Efficiency and hygiene review of Kubernetes manifests for build and test workloads on GKE:
  missing or oversized requests versus the company's build size tiers, memory limits, lifecycle
  (Job TTL and deadlines, test-environment teardown TTL, replica caps), spot pool placement,
  ephemeral storage, image pinning, sidecars, PVCs, privileged containers and labels. Works on
  plain YAML, `helm template` output and `kustomize build` output. Use before merging a build
  manifest or TeamCity build template, in PR review, and as a CI gate.
allowed-tools: Bash(*/k8s-manifest-review/scripts/run.sh *), Bash(python3 */k8s-manifest-review/scripts/k8sreview.py *), Bash(helm template *), Bash(kustomize build *), Bash(kubeconform *), Read, Glob, Grep
---

# k8s-manifest-review: requests are money

`scripts/run.sh` (or `python3 scripts/k8sreview.py`) reviews manifests deterministically against
a per-repo policy and the build size tiers produced by `gke-cost-discovery`. Exit code 1 at the
`--fail-on` gate (default `high`) makes it a CI step. Rule catalogue: `reference/rules.md`.

```bash
scripts/run.sh PATH... [--policy k8s-review.json] [--tiers build-tiers.json] [--fail-on high] [--build] [--no-tools] [--json]
helm template my-chart -f values-test.yaml | scripts/run.sh - --build
kustomize build overlays/test | scripts/run.sh - --build
```

Policy discovery: `k8s-review.json` and `build-tiers.json` next to the manifests or in the
current directory. `--build` treats every workload as a build/test workload (otherwise only
those in `build_namespaces` or carrying the tier label).

## Policy (`k8s-review.json`)

```json
{
  "tiers_file": "build-tiers.json",
  "default_tier": "medium",
  "tier_label": "build.company.io/tier",
  "build_namespaces": ["teamcity-agents", "test-envs"],
  "ttl_annotation": "janitor/ttl",
  "max_test_replicas": 2,
  "spot_selector": {"cloud.google.com/gke-spot": "true"},
  "required_labels": ["app", "owner"],
  "max_containers": 3,
  "disabled_rules": [],
  "severity_overrides": {"EFF010": "low"}
}
```

## Procedure

1. Render first when needed: Helm templates and Kustomize overlays are reviewed as rendered
   output, never as templates (files containing `{{` are reported and skipped).
2. Run the review. Read `high` first: no requests, requests above the tier, missing Job TTL,
   test environments without a teardown TTL, privileged containers.
3. For each finding, propose the exact YAML change. For request sizes use the discovery
   report's recommendation for that workload when one exists (p95 x headroom); otherwise the
   tier value. Never invent numbers.
4. Re-run until the gate passes; report the before/after findings counts and the total requests
   before/after (cpu, memory) so the saving is visible.
5. Schema validation: `kubeconform` when installed (offline), else `kubectl apply
   --dry-run=client` only if a cluster is reachable; the review says which ran.

## What "good" looks like for a build Job

requests set and within tier, memory limit = request, `ephemeral-storage` requested,
`ttlSecondsAfterFinished`, `activeDeadlineSeconds`, `backoffLimit: 1`, pinned image with
`IfNotPresent`, `nodeSelector`/toleration for the spot pool, `emptyDir` with `sizeLimit`, no PVC,
no privileged/hostPath, labels `app`, `owner`, and the tier label. Test-environment Deployments
add the TTL annotation and keep replicas at 1.
