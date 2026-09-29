---
name: "Skeleton a large file instead of reading it whole"
tags: ["triggering", "code-skeleton"]
runs: 3
max_turns: 8
timeout_seconds: 300
allowed_tools: ["Bash", "Read", "Glob", "Grep", "Skill"]
---
What is in `src/gateway.py`? Give me the classes and methods it defines.
