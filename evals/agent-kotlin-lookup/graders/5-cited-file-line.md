---
type: regex
target: "last_message"
pattern: '(Checkout|OrderServiceTest)\.kt:\d+'
match: "contains"
---
AGENT.md requires `file:line` citations. On a fixture with two same-named methods, the line
reference is also what lets a developer confirm which `place` was meant without re-running
anything.
