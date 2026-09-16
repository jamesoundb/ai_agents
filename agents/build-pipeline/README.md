# Build Pipeline Agent (TeamCity)

**Purpose.** Make TeamCity build templates cheap, bounded and reproducible, and turn red builds
into named causes with evidence. Works with the kubernetes agent (tiers, guardrails, discovery)
because the build templates are what size the cluster's pods.

| Ask it | It returns |
|---|---|
| "Review this .teamcity change" | Findings (timeouts, cleanup, triggers, secrets, artifact rules, caches, teardown), pod-template capacity table, exact DSL/XML edits |
| "Why did build 12345 fail?" | Failure class with quoted evidence, failed tests (new marked), routing (infra vs deps vs code), no blind reruns |
| "How healthy/expensive is CI?" | Failure-class histogram over recent builds, queue vs duration, builds without timeouts, environments left up |

Not for: triggering or editing builds on the server, deploying, cluster changes.

## Skills

| skill | role |
|---|---|
| [`teamcity-config-review`](../../skills/teamcity-config-review/SKILL.md) | text rules over Kotlin DSL/XML; embedded pod templates through `k8s-manifest-review` |
| [`teamcity-build-triage`](../../skills/teamcity-build-triage/SKILL.md) | REST or offline log classification; recent-failures histogram |
| [`k8s-manifest-review`](../../skills/k8s-manifest-review/SKILL.md), [`gke-cost-discovery`](../../skills/gke-cost-discovery/SKILL.md) | tiers and demand/capacity evidence |
| `code-graph`, `code-skeleton` | navigate build scripts and repositories |

Requirements: Python 3.10+ (shared tree-sitter venv with `code-graph`); a read-only TeamCity
access token for live triage (offline mode
works from a saved build log and build JSON).

## Inputs to configure

TeamCity URL and read-only token; one real build template and the Kubernetes cloud profile pod
templates; confirmation that settings are versioned (Kotlin DSL). Until then, fixtures under the
worklog demonstrate every rule and failure class.

## Verified (2026-09-07)

- Settings fixture (Kotlin DSL with a Kubernetes cloud image pod template, two build types; XML
  build type and project): plain-text secrets, missing timeouts, deploy-without-teardown,
  replicas 4, `+:**` artifacts, no cleanup, unfiltered triggers, `latest` images, missing caches,
  and the pod template's 8 cpu / 32 GiB above tier were all reported; a correctly filtered
  trigger is not flagged.
- Triage fixtures for tests, OOM, agent scheduling, dependency 404, compile, config, timeout and
  unknown each classify correctly with evidence; no-token runs fail with a clear message.
