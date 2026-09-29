---
name: "ast-treesitter resolves Python callers that text search cannot see"
agent: ast-treesitter
tags: ["agent", "python", "resolution"]
runs: 3
max_turns: 10
timeout_seconds: 420
allowed_tools: ["Bash", "Read", "Glob", "Grep", "Skill"]
---
Who calls `Ledger.post`?
