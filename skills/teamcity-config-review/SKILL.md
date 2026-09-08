---
name: teamcity-config-review
description: >
  Review TeamCity project settings (Kotlin DSL settings.kts/*.kt or XML project-config and
  buildTypes) for cost and hygiene: execution timeouts, cleanup rules, VCS trigger branch
  filters and quiet periods, concurrency caps, artifact rules, plain-text secrets, unpinned step
  images, build caches, builds that create Kubernetes test environments without a teardown, and
  Kubernetes cloud-image pod templates reviewed against the build size tiers. Use for changes
  under .teamcity/, when builds are slow or expensive, and before adding a build configuration.
allowed-tools: Bash(python3 */teamcity-config-review/scripts/tcreview.py *), Bash(*/teamcity-config-review/scripts/tcreview.py *), Read, Glob, Grep
---

# teamcity-config-review: the build template is a cost decision

```bash
scripts/tcreview.py .teamcity [--policy tc-review.json] [--fail-on high] [--json]
scripts/tcreview.py .teamcity/settings.kts .teamcity/Api/buildTypes/*.xml
```

Rules are text-based over the DSL/XML (no TeamCity server needed) and scoped per build
configuration, project, template or Kubernetes cloud image. Pod templates embedded in cloud
images (`podSpecification = customTemplate { customPod = """..."""}`) or XML CDATA are extracted
and reviewed by `k8s-manifest-review` with the build tiers; those findings appear as
`TC005-<rule>`. Catalogue: `reference/rules.md`.

## Policy (`tc-review.json`, optional)

```json
{
  "max_execution_timeout_min": 180,
  "require_branch_filter": true,
  "k8s_review_policy": "k8s-review.json",
  "teardown_patterns": ["kubectl delete", "helm uninstall", "teardown"],
  "deploy_patterns": ["kubectl apply", "kubectl scale", "helm upgrade", "helm install"]
}
```

## Procedure

1. Run on the whole `.teamcity` directory first; then on the changed files in a PR.
2. `critical`/`high` first: plain-text secrets, missing execution timeout (a hung build holds
   a pod all night), builds that deploy test environments without a teardown step, pod
   templates above tier or without requests.
3. Propose exact DSL/XML edits. For pod templates use the discovery report's recommended
   requests; for teardown add a final step with `executeStep = ALWAYS` (Kotlin
   `executionMode`), or rely on the namespace janitor and annotate the environment.
4. Re-run until the gate passes; report the before/after findings and, for pod templates, the
   before/after requests per agent (cpu, memory) multiplied by `maxInstancesCount` so the
   capacity effect is visible.

## Limits

Text rules cannot see settings kept only in the TeamCity UI (non-versioned projects) or
inherited from templates defined elsewhere; export settings to Kotlin DSL (Project settings ->
Versioned Settings) so they can be reviewed. Steps that call scripts stored in the repository
are reviewed only for the inline part; open the script for the rest.
