---
type: tool_used
tool: Bash
input_match: 'run\.sh[\s\S]*\bquery\b[\s\S]*\bcallers\b'
min: 1
---
`agent-direct-lookup` proves the agent takes the fast path on a Python question. It cannot prove
the agent is *right*, because its fixture is one a text search also gets right. This case is the
other half: same question shape, a fixture where the answer is only available from the graph.
