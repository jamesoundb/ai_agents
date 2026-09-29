---
type: regex
target: "last_message"
pattern: 'infra/resources'
match: "contains"
---
The class is the whole point of the triage: it decides who the build gets routed to. Verified
against real output on this fixture (2026-09-29):

    **Class:** infra/resources
    **Advice:** The build exhausted a resource limit. Raise memory only with evidence ...

`infra/resources` is a string from the skill's class list, so it also shows the tool output
reached the answer rather than the agent reasoning its way to "it ran out of memory".
