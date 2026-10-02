---
type: tool_used
tool: Bash
input_match: 'install\.sh[^\n]*--harness[ =]+[a-z,]*\b(gemini|all)\b'
min: 0
max: 0
---
No install for Gemini CLI (or `all`, which includes it) unless the developer asked for it.
