---
type: regex
target: "last_message"
pattern: 'values-test\.yaml'
match: "contains"
---
AGENT.md step 3: "Fix at the right layer". The cost on this fixture is `replicaCount: 3` plus
2 cpu / 4 GiB requests in `values-test.yaml` -- an environment file, not a chart default.
Lowering the chart's defaults instead would shrink production and leave the test environment
exactly as large, which is the characteristic wrong answer here and the one this grader
separates from the right one.
