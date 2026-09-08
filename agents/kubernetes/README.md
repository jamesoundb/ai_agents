# Kubernetes Agent (GKE, cost-efficiency first)

**Purpose.** Stop overprovisioning a GKE Standard cluster that runs test builds and test
environments, with evidence rather than guesswork, and keep it that way with tiers and admission
guardrails. Also the home for Kubernetes manifest and dependency questions.

## The problem it addresses

Developers edit build manifests (typically CI build templates), set replica counts and press
build. Autoscaling is on, yet billing shows unused and unallocated capacity. Those are two
different problems:

| bucket | definition | typical cause | fix owner |
|---|---|---|---|
| unused | requested but not consumed | requests copied from a generous template | manifests (developers) |
| unallocated | node capacity nobody requested | node pool minimums, oversized machine shapes, slow/blocked scale-down, on-demand pools for burst work | cluster (platform, via Terraform) |

Lifecycle waste (test environments left up, Jobs without TTL, completed pods lingering) shows up
in both buckets and is usually the cheapest win.

## Skills

| skill | role |
|---|---|
| [`gke-cost-discovery`](../../skills/gke-cost-discovery/SKILL.md) | namespaces ranked by cost (estimated, or actual from the billing export) with per-namespace mitigation levers; read-only collection (gcloud, kubectl, Cloud Monitoring, Cloud Logging, TeamCity REST) and a report: two-bucket waste model, top over-requested workloads with recommended requests, stale environments, scale-down blockers, node pool review, autoscaler reasons, TeamCity demand, proposed build tiers |
| `code-graph`, `blast-radius`, `code-skeleton` | Kubernetes object graph (Service to Deployment selection, ConfigMap/Secret/PVC references, Kustomization imports) for change impact |

| [`k8s-rightsize`](../../skills/k8s-rightsize/SKILL.md) | rewrite a namespace's build/test manifests from the report's p95 (or the tier when no usage): diff + evidence + lifecycle fields |
| [`k8s-manifest-review`](../../skills/k8s-manifest-review/SKILL.md) | tier/lifecycle/hygiene rules on plain YAML, `helm template` or `kustomize build` output; CI gate |
| [`k8s-guardrails`](../../skills/k8s-guardrails/SKILL.md) | LimitRange, ResourceQuota and optional namespace janitor generated from `build-tiers.json` |
| [`helm-chart-review`](../../skills/helm-chart-review/SKILL.md) | chart/values/GitOps review with a rendered-manifest pass (shared with the helm agent) |

## How a developer gets a recommendation

1. Install the agents (`install-agents` skill) in the manifests repository.
2. Ask the kubernetes agent to right-size the build or test job. It uses the published discovery
   report if the platform team committed one (`discovery/latest/report.json`, `build-tiers.json`),
   otherwise it offers a local measurement on minikube/kind with the real test suite, and only as
   a last resort tier capping.
3. It returns the diff, the evidence per container (p95 CPU, peak memory, window), the requests
   freed per pod, and the manifest review result. The developer commits the change; guardrails and
   kube-janitor keep it from regressing.

## Inputs to configure

Copy `skills/gke-cost-discovery/gke-cost-discovery.env.example` and provide: `GCP_PROJECT`,
`GKE_CLUSTER`, `GKE_LOCATION`, `BUILD_NAMESPACES`, read-only credentials (roles
`container.clusterViewer`, `monitoring.viewer`, `logging.viewer`, cluster RBAC `view`),
`TEAMCITY_URL` and a read-only `TEAMCITY_TOKEN`, plus one real build manifest. Without them the
skill still runs and reports exactly which inputs are missing; `scripts/synth.py` produces a
demo dataset.

## Verified (2026-09-07)

- Synthetic 14-node / 31-pod / 468-build dataset: report separates unused (~$166/month) from
  unallocated (64% of allocatable CPU), lists over-requested build agents with recommended
  requests, four stale test environments, Jobs without TTL, a bare pod blocking scale-down,
  autoscaler no-scale-down reasons, TeamCity peak concurrency, and proposes tiers.
- Collector with no credentials writes `MISSING-*` notes and the analyzer still produces a
  report listing what to provide; against a real (minikube) cluster the kubectl snapshot path
  parses nodes, pods, workloads, quotas and `kubectl top`.
- Guardrails applied to a minikube namespace: a pod without requests receives the small-tier
  defaults, a pod above the large tier is rejected at admission, the quota accounts for usage,
  and the janitor deleted an environment past its `janitor/ttl` while keeping one marked
  `forever`. Two defects were found only by this live test (LimitRange ratio arithmetic, an
  unpullable kubectl image) and fixed.

Dollar figures are list-price approximations for ranking; the billing export is the authority.
