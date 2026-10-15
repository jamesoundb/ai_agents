---
type: regex
target: "last_message"
pattern: '(?i)(not\s+(be\s+)?enforc|isn.t\s+enforc|aren.t\s+(being\s+)?enforc|doesn.t\s+enforce|does\s+not\s+enforce|without\s+enforc|no\s+policy-enforcing|kindnet)'
match: "contains"
---
Why the question matters: every `reach` result on this snapshot ends with

    enforcement: no policy-enforcing CNI found in kube-system: these policies may not be enforced at all

The current cluster runs kindnet, so "it works today" proves nothing about Dataplane V2. An answer
that misses this cannot explain why paths that work now break after the move.
