---
type: tool_used
tool: Bash
input_match: 'run\.sh\s+query\s+overview'
min: 0
max: 0
---
The load-bearing assertion for the agent's workflow.

AGENT.md lists seven numbered steps (build, orient, locate, traverse, impact, verify, answer).
Written as a sequence they read as mandatory, and the agent ran `overview` and hub ranking
before reaching the one query that answers a direct lookup. The prompt now opens with "these
are the moves available, not a sequence to complete" and names the fast path: a direct lookup
is build plus one query.

`overview` is the marker for that failure -- it is the orientation step, and orientation is
for open-ended questions where you do not yet know what to look at. Its absence here is what
distinguishes a fast path from a pipeline.
