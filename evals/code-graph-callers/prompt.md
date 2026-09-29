---
name: "Find callers through the graph rather than grepping"
tags: ["triggering", "code-graph"]
runs: 3
max_turns: 10
timeout_seconds: 300
allowed_tools: ["Bash", "Read", "Glob", "Grep", "Skill"]
---
Who calls `OrderService.place`?
