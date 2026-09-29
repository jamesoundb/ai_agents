---
type: regex
target: "last_message"
pattern: '(OOMKilled|exit code 137|code 137)'
match: "contains"
---
AGENT.md: "Quote the class and evidence"; the skill: "do not paraphrase the log". The log line
the classifier fired on is:

    8: [08:51:58] : Container tests exceeded its memory limit and was terminated: OOMKilled

A developer who is told "it failed for infrastructure reasons" reruns it anyway. One who is
shown the OOMKill does not.
