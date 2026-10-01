---
type: tool_used
tool: Bash
input_match: 'run\.sh[\s\S]*\bquery\b[\s\S]*\bcallers\b'
min: 1
---
Same assertion as `agent-direct-lookup`, on Kotlin instead of Python. The engine's Kotlin
coverage is the deepest in the regression suite (43 labelled assertions against 10 for Java),
but that proves the *graph* is right about Kotlin, not that the agent reaches for it when the
question arrives in Kotlin. This case exists because those are different claims and only the
first was evidenced.
