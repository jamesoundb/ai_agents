---
type: tool_used
tool: Bash
input_match: '\bgit\s+clone\b'
min: 0
max: 0
---
A plain `git clone` skips the reference file and the identity copy that `ai-task clone` makes,
and puts the repository wherever the agent happens to be.
