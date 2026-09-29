---
name: "A tiny file is read, not skeletonized"
tags: ["negative", "code-skeleton", "never-worse"]
runs: 3
max_turns: 6
timeout_seconds: 300
allowed_tools: ["Bash", "Read", "Glob", "Grep", "Skill"]
---
What version does `src/version.py` declare?
