---
name: "kubernetes agent traces several failures to one lost node"
agent: kubernetes
tags: ["agent", "kubernetes", "troubleshooting", "hard-limits"]
runs: 3
max_turns: 14
timeout_seconds: 720
allowed_tools: ["Bash", "Read", "Glob", "Grep", "Skill", "Edit", "Write"]
---
Since this morning `on-m02` in `kg-faults` has no available pods, and `kindnet` and `kube-proxy` in
kube-system show 1/2 ready. A teammate saved the cluster state with kubegraph into
`cluster-snapshot/`; we have no cluster access from here. Our manifests are in `manifests/`. What is
going on, and what do we fix?
