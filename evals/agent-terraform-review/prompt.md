---
name: "terraform agent gates a merge on the deterministic review"
agent: terraform
tags: ["agent", "terraform", "review"]
runs: 3
max_turns: 12
timeout_seconds: 420
allowed_tools: ["Bash", "Read", "Glob", "Grep", "Skill"]
---
Is `environments/sandbox` safe to merge?
