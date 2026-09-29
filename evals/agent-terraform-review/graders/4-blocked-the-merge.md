---
type: regex
target: "last_message"
pattern: '(?i)(\bblock\b|do not merge|not safe to merge|fail(s|ed)?\b)'
match: "contains"
---
The gate on this fixture is FAIL (two critical, two high at `--fail-on high`). The question
asked for a verdict, and AGENT.md requires one: "approve", "approve with conditions" or
"block", with the conditions spelled out. An answer that lists findings and trails off has
not answered it.
