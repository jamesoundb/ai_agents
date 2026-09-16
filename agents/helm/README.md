# Helm Agent (GitOps: ArgoCD / Flux)

**Purpose.** Charts that render correctly in every environment, environments that request
exactly what they need, and delivery objects that promote by version bump. Works with the
kubernetes agent's tiers and guardrails so test environments stay small and short-lived.

| Ask it | It returns |
|---|---|
| "Review this chart PR" | Findings across chart / values / GitOps layers, helm lint, rendered-manifest review, exact YAML fixes |
| "Why is the test environment for api so big?" | Effective values per environment file, ArgoCD parameters that override them, tier comparison, right-sized values |
| "Promote api to 1.4.3 in prod" | Pinned revision diff, rendered cluster diff, rollback path |
| "Is this Application/HelmRelease safe?" | Revision pinning, project, prune/selfHeal, TTL, remediation, secrets checks |

Not for: installing or upgrading releases, cluster operations, platform infrastructure.

## Skills

| skill | role |
|---|---|
| [`helm-chart-review`](../../skills/helm-chart-review/SKILL.md) | chart/values/GitOps rules, helm lint, render + `k8s-manifest-review` on the output; effective-values table |
| [`k8s-manifest-review`](../../skills/k8s-manifest-review/SKILL.md) | tier/lifecycle/hygiene rules on rendered manifests |
| `code-graph`, `blast-radius`, `code-skeleton` | find charts, values and GitOps objects; Kubernetes object references |

Requirements: Python 3.10+ (PyYAML optional; the tree-sitter fallback and `code-graph` share one
venv), Helm 3 for lint/render.

## Verified (2026-09-07)

Fixture chart (`helm create` mutated: unpinned dependencies, latest tag, pullPolicy Always, no
resources, a password in values-prod, replicaCount 4 in values-test) plus an ArgoCD Application
with `targetRevision: HEAD`, `project: default`, replicaCount parameter 6 into a test namespace,
no TTL, and a Flux HelmRelease with unpinned chart version, 1m interval and infinite retries:
every planted issue reported; renders succeed with the parent-chart fallback when subcharts are
not fetched; rendered manifests reviewed against the tiers.
