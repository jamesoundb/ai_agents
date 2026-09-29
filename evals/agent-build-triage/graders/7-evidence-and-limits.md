---
type: regex
target: "last_message"
pattern: '(?is)evidence.{0,4000}limits'
match: "contains"
---
The persona discriminator, and the one grader here that no skill can satisfy.

The rest of this case overlaps with `teamcity-build-triage/SKILL.md`, which also classifies,
also routes `infra/*` to the kubernetes agent and also says a rerun changes nothing -- so those
graders would largely pass without `--agent`. "End with Evidence and Limits (settings not in
version control, missing token, log truncation)" appears only in
`agents/build-pipeline/AGENT.md`.

One confound, checked: `teamcity-config-review/SKILL.md` has a `## Limits` section of its own.
It documents what the *rules* cannot see rather than instructing an output section, and this
case uses `teamcity-build-triage`, not that skill. Requiring "evidence" before "limits" keeps
a stray echo of that heading from satisfying the grader on its own.
