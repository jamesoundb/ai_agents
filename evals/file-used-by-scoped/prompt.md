---
name: "Rank a file's symbols by their users in one part of the repo"
tags: ["triggering", "code-graph", "efficiency"]
runs: 3
max_turns: 10
timeout_seconds: 300
allowed_tools: ["Bash", "Read", "Glob", "Grep", "Skill"]
---
Which functions and classes in `core/util.py` does the application code under `app/` rely on most? Rank them by how many files in `app/` use each one.
