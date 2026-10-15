---
type: regex
target: "last_message"
pattern: '(?is)agents-test-m02.{0,400}(NotReady|not\s+ready|kubelet)|(NotReady|not\s+ready|kubelet).{0,400}agents-test-m02'
match: "contains"
---
The shared cause, verified with `why deploy/on-m02` (2026-10-15):

    cause: Node/agents-test-m02 NodeNotReady: Ready=Unknown Kubelet stopped posting node status.

`health` lists it first under (cluster-scoped), and every failing pod in the prompt is `NodeLost` on
that node.
