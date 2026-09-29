---
type: tool_used
tool: Bash
input_match: 'run\.sh\s+query\s+overview'
min: 0
max: 0
---
The workflow fast path: a direct lookup is build plus one query, not the full seven-step
sequence. `overview` is the orientation step and orientation is for questions where you do not
yet know what to look at. Carried over from `agent-direct-lookup` so a regression in the
AGENT.md workflow shows up on both languages rather than only on Python.
