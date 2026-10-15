---
name: "kubernetes agent checks NetworkPolicy paths before a move to an enforcing cluster"
agent: kubernetes
tags: ["agent", "kubernetes", "networkpolicy", "hard-limits"]
runs: 3
max_turns: 14
timeout_seconds: 720
allowed_tools: ["Bash", "Read", "Glob", "Grep", "Skill", "Edit", "Write"]
---
We are moving the `kg-net` namespace to our GKE cluster, which runs Dataplane V2 and enforces
NetworkPolicy. The policies in `manifests/kg-net.yaml` are what runs on the current cluster today; a
teammate saved its state with kubegraph into `cluster-snapshot/` (no cluster access from here).
After the move: will `frontend` still reach `api`, will `api` reach `db`, and can the `tester`
deployment run its smoke tests against `api`? Fix the policies so those three paths work and nothing
else opens up.
