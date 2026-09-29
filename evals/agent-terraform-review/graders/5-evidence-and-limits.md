---
type: regex
target: "last_message"
pattern: '(?is)evidence.{0,4000}limits'
match: "contains"
---
The persona discriminator.

"End with Evidence (commands run) and Limits (what static analysis could not determine)"
appears in `agents/*/AGENT.md` and nowhere in any `SKILL.md` -- so this grader fails when the
case runs without `--agent`, which is the mistake the whole agent-coverage effort exists to
prevent. Ordered (`evidence` before `limits`) because the contract is about how the answer
ends, and bounded so two unrelated mentions pages apart do not satisfy it.
