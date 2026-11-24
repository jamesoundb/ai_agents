---
type: tool_used
tool: Bash
input_match: 'dungeon-map/scripts/(run\.sh|dungeonmap\.py)[\s\S]*\bbuild\b[\s\S]*--from\b'
min: 1
---
`build --from org-snapshot` is the only way to read a `--save` directory as a graph (usage.md).
