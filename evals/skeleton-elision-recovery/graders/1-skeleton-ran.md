---
type: tool_used
tool: Bash
input_match: 'run\.sh\s+(skeleton|query)'
min: 1
---
The question must be answered through the engine rather than by reading the file.

Originally this required `run.sh skeleton` specifically, and it failed a run that was
entirely correct: the agent used `query callees PaymentGateway.charge` and produced the
complete list, including the call the cap hides. `callees` is the purpose-built query for
"what does X call" and is arguably the better route.

Grader 2 was widened to accept `callees` for exactly this reason; leaving this one pinned
to `skeleton` was an inconsistency that punished good behavior. Three runs surfaced it;
one did not.
