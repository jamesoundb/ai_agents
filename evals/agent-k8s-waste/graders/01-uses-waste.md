---
type: tool_used
tool: Bash
input_match: 'cluster-graph/scripts/(run\.sh|kubegraph\.py)\b.*\bwaste\b'
min: 1
---
SKILL.md: "what the cluster wastes now (~$/month) | `waste`". One query itemizes idle requests,
over-requests, unmounted disks, load balancers, lifecycle gaps, scale-down blockers and underfilled
nodes with list-price estimates; `gke-cost-discovery` needs a live cluster this workspace does not have.
