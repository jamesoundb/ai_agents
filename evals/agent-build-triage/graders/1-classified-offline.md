---
type: tool_used
tool: Bash
input_match: 'tctriage\.py.*--build-json'
min: 1
---
Two assertions in one, both from AGENT.md.

*Classified*: "`teamcity-build-triage` on the build id (or the saved log)" -- reading the log
by eye is the behaviour being replaced, because it produces a paraphrase instead of a class.

*Offline*: the prompt says there is no token, and the skill's offline form
(`--build-json build.json --log build.log`) is the documented answer to that. An agent that
misses it stalls asking for `TEAMCITY_URL`, which is the observable failure this catches.
