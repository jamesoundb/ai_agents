---
type: regex
target: "last_message"
pattern: '(?i)(queue|1500 ?s|25 ?min)'
match: "contains"
---
The systemic-waste half of the mandate. The triage output on this fixture reads:

    Duration 900s, queue 1500s (queue time > duration: capacity problem, not a code problem)

AGENT.md step 3 makes that observation reportable: queue time larger than duration means the
agent pool is undersized for peak, which is a cluster finding, not a build finding. It is in
the tool output, so this grader measures whether the agent read past the first line of it.
