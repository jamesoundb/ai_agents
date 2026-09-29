---
type: regex
target: "last_message"
pattern: '(api|jobs|core|test_core)\.py:\d+'
match: "contains"
---
AGENT.md requires `file:line`. On a fixture whose whole point is that the calling files do not
contain the searched name, the citation is what lets a developer check the answer at all.
