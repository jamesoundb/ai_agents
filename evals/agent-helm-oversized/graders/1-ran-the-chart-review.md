---
type: tool_used
tool: Bash
input_match: 'helm-chart-review/scripts/helmreview\.py'
min: 1
---
AGENT.md step 2: run `helm-chart-review` with the chart, the GitOps objects and `--env-values`
for the environment in question, and read the effective-values table before the findings.

"Why is this environment so big" is precisely the question the effective-values table answers,
and it is a question `helm template` alone answers badly: the size comes from a values file
layered on top of chart defaults, which is what the table resolves.
