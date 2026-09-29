---
type: tool_used
tool: Bash
input_match: 'terraform-review/scripts/(run\.sh|tfreview\.py)'
min: 1
---
"Is this safe to merge" is a gate question, and the agent has a gate for it. AGENT.md step 2:
review with `terraform-review` before declaring anything done. Answering from a read of the
HCL would reach some of the same conclusions by recall -- which is exactly the failure mode,
because recall has no gate result and no rule catalogue behind it.
