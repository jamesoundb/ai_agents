---
type: tool_used
tool: Grep
min: 0
max: 0
---
Observed before the workflow fix: the agent answered correctly from the graph, then ran
`grep -rn "place" src/ tests/` and two MagicMock scripts to "verify empirically" -- replacing
a resolved, confidence-labelled edge with a weaker text match. AGENT.md now says so in as many
words. This asserts it does not happen.
