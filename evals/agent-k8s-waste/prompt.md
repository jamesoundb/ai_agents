---
name: "kubernetes agent itemizes current cluster waste from a saved snapshot"
agent: kubernetes
tags: ["agent", "kubernetes", "cost", "hard-limits"]
runs: 3
max_turns: 14
timeout_seconds: 720
allowed_tools: ["Bash", "Read", "Glob", "Grep", "Skill", "Edit", "Write"]
---
Finance flagged the cost of our test cluster. A teammate saved its state with kubegraph into
`cluster-snapshot/`; we have no cluster access from here. What is it wasting right now, roughly what
does each item cost, and what do we change? The preview environment's manifests are in `manifests/`.
