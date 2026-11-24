---
name: "terraform agent gates a merge on the deterministic review"
tags: ["agent", "terraform", "review"]
runs: 3
max_turns: 15
timeout_seconds: 420
allowed_tools: ["Bash", "Read", "Glob", "Grep", "Skill"]
---
Is `environments/sandbox` safe to merge?
