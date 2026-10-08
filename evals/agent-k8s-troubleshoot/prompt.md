---
name: "kubernetes agent finds root causes from a saved cluster snapshot"
agent: kubernetes
tags: ["agent", "kubernetes", "troubleshooting", "hard-limits"]
runs: 3
max_turns: 16
timeout_seconds: 720
allowed_tools: ["Bash", "Read", "Glob", "Grep", "Skill", "Edit", "Write"]
---
In namespace `kg-faults` the `uses-pvc` and `missing-config` deployments never start, and the `shop`
ingress returns 503s. I have no cluster access from here; a teammate saved the cluster state with
kubegraph into `cluster-snapshot/`. Find out why and fix it. Our manifests are in `manifests/`.
