---
type: tool_used
tool: Bash
input_match: 'run\.sh[\s\S]*\bfile\b[\s\S]*--used-by'
min: 1
---
`query file PATH --used-by` ranks a file's symbols by distinct other files using them in one
call. Measured on TensorFlow's 6,289-line `ops.py` before it existed (2026-10-01): the model
wrote a script running `callers` per symbol, ~5 extra turns per question.
