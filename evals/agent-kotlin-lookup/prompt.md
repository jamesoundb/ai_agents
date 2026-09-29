---
name: "ast-treesitter resolves a Kotlin caller past a same-named decoy"
agent: ast-treesitter
tags: ["agent", "kotlin", "workflow", "resolution"]
runs: 3
max_turns: 10
timeout_seconds: 420
allowed_tools: ["Bash", "Read", "Glob", "Grep", "Skill"]
---
Who calls `OrderService.place`?
