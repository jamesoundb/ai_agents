---
type: llm
target: "trace"
criteria: |
  The task is to read one value out of a two-line file.

  PASS if the assistant reached the answer in one or two tool calls — typically a single
  file read, optionally preceded by one directory listing.
  PASS if it ran the code-skeleton once, saw the "source is no larger than its skeleton,
  shown in full" guard line, and answered straight from that output.
  FAIL if it built the relationship graph, or needed three or more tool calls, to answer a
  question this small.

  Judge only the tool calls in the trace. Do not reward or penalise the wording of the
  final answer.
focus: "proportionate effort for a two-line file"
---
Over-triggering costs tokens too. This is the counterweight to the triggering cases, and it
is the only grader here that would notice the engine being used *well* but too eagerly.

History worth keeping, because this grader was removed once and restored:

It originally failed 2 of 3 runs, and the reason was environmental rather than behavioural.
Antigravity loads the operator's `~/.gemini/GEMINI.md` into every session; that file says
"verifying your results empirically is also mandatory" and asks the agent to maintain a
`.github/instructions.md`. So the agent read `version.py`, obtained the answer, and then
spent several more calls confirming it and tending a worklog — obeying its operator, not
misusing these skills. The count was measuring the wrong thing.

The fix was to isolate the environment, not to weaken the assertion: `run_agy.py` now runs
each case under a HOME that carries the credentials and installed skills but not the
operator's instruction file (`--use-global-context` opts back in). Under isolation the same
case completes in **one** tool call, so the threshold above is comfortable rather than tight.

If this starts failing again, check whether isolation is still in effect before assuming the
skills regressed.
