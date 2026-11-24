---
type: regex
target: "last_message"
pattern: 'evidence.{0,4000}limits'
flags: "is"
match: "contains"
---
The persona discriminator -- see the same grader in `agent-terraform-review`. "End with
Evidence (commands and files) and Limits (approximate pricing, missing inputs, metrics
window)" lives in `agents/kubernetes/AGENT.md` and in no `SKILL.md`, so this grader is what
fails if the case ever stops passing `--agent`.
