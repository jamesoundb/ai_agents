---
type: tool_used
tool: Bash
input_match: 'run\.sh[\s\S]*\bskeleton\b'
min: 1
---
The skill's whole point is to index before reading. Graded on the Bash call to the
engine rather than on the `Skill` tool, because the observable effect is
harness-independent: some harnesses surface the skill as a slash command, others
inline its instructions.
