---
name: "ast-treesitter takes the fast path for a direct lookup"
tags: ["agent", "workflow", "proportionality"]
runs: 3
max_turns: 10
timeout_seconds: 420
allowed_tools: ["Bash", "Read", "Glob", "Grep", "Skill"]
---
Who calls `OrderService.place`?
