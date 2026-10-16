---
type: tool_used
tool: Bash
input_match: 'guardrails\.py[^\n]*--concurrent-pods[ =](1[5-9]|2[0-8])\b'
min: 1
---
SKILL.md step 1: "Choose `--concurrent-pods` from the report's TeamCity peak concurrency, with some
margin." The report says `peak_concurrency: 14`; the script's default (20) only passes when it is
passed on purpose. 15-28 is "some margin" up to twice the peak.
