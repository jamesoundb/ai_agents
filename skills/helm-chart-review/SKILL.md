---
name: helm-chart-review
description: >
  Review Helm charts and their GitOps delivery objects together: Chart.yaml and dependency
  pinning, values.yaml defaults (resources, image tag, pull policy, secrets), per-environment
  values layering and effective replica/resource settings, dead values, ArgoCD Application and
  Flux HelmRelease hygiene (pinned revisions, projects, prune/selfHeal, TTL for test
  environments, remediation), plus helm lint and a rendered-manifest pass through
  k8s-manifest-review. Use for chart PRs, GitOps changes, "why is this environment so big",
  and before promoting a chart version.
allowed-tools: Bash(python3 */helm-chart-review/scripts/helmreview.py *), Bash(*/helm-chart-review/scripts/helmreview.py *), Bash(helm lint *), Bash(helm template *), Bash(helm show *), Bash(helm dependency build *), Read, Glob, Grep
---

# helm-chart-review: chart, values and delivery in one pass

```bash
scripts/helmreview.py CHART_DIR [GITOPS_FILES_OR_DIRS ...] [--env-values values-test.yaml] \
                      [--values-glob 'values*.yaml'] [--policy helm-review.json] [--fail-on high] [--no-tools] [--json]
scripts/helmreview.py - gitops/          # GitOps objects only
```

Layers reviewed (rule prefixes): `HC` chart structure, `HV` values, `HG` GitOps
(ArgoCD `Application`, Flux `HelmRelease`), `HL` helm lint, `HR-*` findings from
`k8s-manifest-review` on the rendered output (medium and above). Catalogue: `reference/rules.md`.

Rendering: `helm template` for the base values and again with `--env-values`; an environment
whose name looks like test/pr/preview/dev is reviewed as a build/test workload (tiers, TTL,
spot placement). If subcharts are not fetched, the parent chart is rendered alone and the
report says so; run `helm dependency build` for a complete render.

## Policy (`helm-review.json`, optional)

```json
{
  "allowed_projects": ["platform", "apps"],
  "require_pinned_revision": true,
  "test_namespaces": ["test-envs"],
  "max_test_replicas": 2,
  "k8s_review_policy": "k8s-review.json"
}
```

## Procedure

1. Run with the chart and every GitOps object that deploys it (`Application`, `HelmRelease`).
   The "Effective values per environment file" table shows what each environment actually asks
   for; compare it with the discovery report before arguing about numbers.
2. `critical`/`high` first: secrets in values, unpinned chart revisions or dependencies,
   test environments without a TTL, rendered pods without requests or above tier.
3. Propose changes at the right layer: chart defaults for everything, environment values for
   sizes, GitOps object for revision/project/sync policy/TTL. Never fix an environment problem in
   the chart's defaults.
4. Re-run until the gate passes; confirm `helm lint` and both renders succeed.
5. State what could not be checked: unfetched subcharts, values injected at deploy time
   (`valuesFrom`, ArgoCD parameters from generators), CRDs not rendered.

## GitOps conventions this encodes

Pinned `targetRevision` / chart `version` (promotion is a version bump); no `default`
AppProject; `prune` on for test namespaces and `selfHeal` with it; test-environment
Applications carry `janitor/ttl` or come from an ApplicationSet pull-request generator so they
vanish with the PR; Flux intervals of 5m or more and bounded remediation retries; secrets never
inline (external-secrets / sealed-secrets / Secret Manager).
