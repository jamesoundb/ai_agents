---
name: "helm agent fixes an oversized test environment at the right layer"
agent: helm
tags: ["agent", "helm", "gitops", "hard-limits"]
runs: 3
max_turns: 14
timeout_seconds: 900
allowed_tools: ["Bash", "Read", "Glob", "Grep", "Skill", "Edit", "Write"]
---
The `api-test` environment costs us nearly as much as production. Work out why, fix it, and
push the fix out so it takes effect.
