---
type: tool_used
tool: Bash
input_match: 'install\.sh[^\n]*--harness[ =]+antigravity\b'
min: 1
---
The session runs in Antigravity (`agy`), so the install is for `antigravity`. Observed on the
first macOS install: the agent chose `--harness gemini`, which `agy` CLI tolerates but which the
Antigravity IDEs never see and which installs no agents.
