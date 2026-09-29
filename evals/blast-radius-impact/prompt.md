---
name: "Blast radius finds the transitive dependent and cites evidence"
tags: ["triggering", "blast-radius", "code-graph"]
runs: 3
max_turns: 12
timeout_seconds: 300
allowed_tools: ["Bash", "Read", "Glob", "Grep", "Skill"]
---
I need to change the signature of `PaymentGateway.charge` in `src/gateway.py`.
What breaks? Which files and symbols depend on it, and which tests should I run?
