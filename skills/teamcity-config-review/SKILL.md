---
name: teamcity-config-review
description: >
  Use for changes under .teamcity/ or slow and expensive builds: TeamCity settings reviewed for
  timeouts, cleanup, triggers, concurrency, secrets, caches and pod templates.
allowed-tools: Bash(python3 */teamcity-config-review/scripts/tcreview.py *), Bash(*/teamcity-config-review/scripts/tcreview.py *), Read, Glob, Grep
---

# teamcity-config-review: the build template is a cost decision

Run the scripts by their installed path from the working directory (for example
`.claude/skills/teamcity-config-review/scripts/...` or
`.agents/skills/teamcity-config-review/scripts/...`), one command per call: no `cd` into a skill
folder, no shell variable, nothing piped or chained after it. Permission rules match the literal
command; Antigravity denies a chain if any part lacks a grant, and installed skill folders are links
outside the working directory. The `Gate` line at the end states pass or fail; do not echo `$?`.

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
