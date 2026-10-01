---
type: tool_used
tool: Bash
input_match: 'run\.sh[\s\S]*\bquery\b[\s\S]*\bcallers\b'
min: 0
max: 2
---
The failure `--used-by` replaces: one `callers` query per symbol (or a script looping over them).
Two are allowed for spot-checking a surprising count; more than that is the per-symbol loop.
