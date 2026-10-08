---
type: tool_used
tool: Bash
input_match: 'cluster-graph/scripts/(run\.sh|kubegraph\.py)\b'
min: 1
---
AGENT.md: "Live state first, from the cluster graph, not from `kubectl get/describe` loops." The
snapshot directory is only readable as a graph through `snapshot --from` (or as raw JSON lists, which
is what this rules out reading by hand).
