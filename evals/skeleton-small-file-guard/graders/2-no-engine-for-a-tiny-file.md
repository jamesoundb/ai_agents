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

This is the deterministic half of the pair. `3-proportionate` judges overall effort, which
needs an LLM and a clean environment; this one asserts the single fact that matters -- the
engine was never invoked for a two-line file -- and cannot flake.
