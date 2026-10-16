---
name: "kubernetes agent sets namespace guardrails and says what they break"
agent: kubernetes
tags: ["agent", "kubernetes", "cost", "guardrails", "hard-limits"]
runs: 3
max_turns: 14
timeout_seconds: 720
allowed_tools: ["Bash", "Read", "Glob", "Grep", "Skill", "Edit", "Write"]
---
Last week one build in `ci-builds` requested a whole node and starved everything else. Put guardrails
on the namespace so that cannot happen again, and get them in place. The platform team's discovery
report is in `discovery/latest/`; the namespace's manifests are in `manifests/`.
