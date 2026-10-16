---
type: tool_used
tool: Bash
input_match: 'k8s-guardrails/scripts/guardrails\.py'
min: 1
---
The LimitRange and ResourceQuota come from `guardrails.py` and the published tiers, not
hand-written YAML: the skill encodes the company defaults (limit = request for memory, the 4x
cpu ratio, the janitor rule) that a hand-written answer drifts from.
