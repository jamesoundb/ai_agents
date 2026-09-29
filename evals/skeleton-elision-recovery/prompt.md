---
name: "Recover elided calls with --max-calls instead of giving up"
tags: ["recovery-contract", "code-skeleton", "regression"]
runs: 5
max_turns: 10
timeout_seconds: 300
allowed_tools: ["Bash", "Read", "Glob", "Grep", "Skill"]
---
List **every** method that `PaymentGateway.charge` calls in `src/gateway.py`.
I need the complete list, not a sample.
