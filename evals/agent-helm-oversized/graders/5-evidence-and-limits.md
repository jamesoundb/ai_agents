---
type: regex
target: "last_message"
pattern: 'evidence.{0,4000}limits'
flags: "is"
match: "contains"
---
The persona discriminator -- see the same grader in `agent-terraform-review`. The
"End with Evidence and Limits" contract exists only in `agents/*/AGENT.md`, so this is the
grader that fails first if the case ever runs without `--agent`.
