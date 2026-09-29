---
type: tool_used
tool: Bash
input_match: 'run\.sh'
min: 0
max: 0
---
The point of the never-worse guard: a two-line file should be read, not put through the
engine. This asserts the engine was never invoked at all.

Deterministic on purpose. The first version of this grader was an `llm` judge counting
total tool calls, and it flaked between PASS and FAIL across runs -- because the count
included a `view_file .github/instructions.md` caused by the operator's global
instruction file (`~/.gemini/GEMINI.md`), not by the task. A grader whose verdict moves
with the operator's personal config is measuring the wrong thing.

A previous `3-proportionate` grader counted *total* task-related tool calls here and was
removed after three runs showed it failing 2 of 3 on behavior we do not control. In the
failing runs the agent read `version.py`, obtained the answer, and then spent several more
calls confirming it -- which is the operator's own global instruction ("verifying your
results empirically is also mandatory") being followed, not a fault in these skills.

Counting an agent's total activity conflates three things: our skills, the operator's
instructions, and the model's disposition. Only the first is ours to test, and this
grader tests it deterministically. Over-triggering of the engine is the real risk and it
is asserted above; general thoroughness is not ours to police.
