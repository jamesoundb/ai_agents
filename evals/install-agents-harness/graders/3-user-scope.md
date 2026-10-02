---
type: tool_used
tool: Bash
input_match: 'install\.sh[^\n]*--scope[ =]+user\b'
min: 1
---
The organization's default is a global install (`--scope user`); install-agents says to pass
`--scope` explicitly because install.sh alone defaults to project scope.
