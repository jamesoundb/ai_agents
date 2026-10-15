---
type: llm
target: "last_message"
criteria: >
  PASS only if the answer names the node agents-test-m02 (NotReady, its kubelet stopped reporting)
  as the one cause of all three symptoms (on-m02, kindnet, kube-proxy), and does not propose
  separate fixes for kindnet or kube-proxy (restarting them, editing their manifests or
  DaemonSets). Recommending that someone with access repair, restart or replace the node or its
  kubelet is correct. FAIL if kindnet or kube-proxy get their own fix, or the node is not named as
  the cause.
---
k8s-cluster-triage's rule: shared causes first. kindnet and kube-proxy are DaemonSets; their pod on
the lost node is `NodeLost` and nothing about them needs changing. A judge, because "do not fix the
symptoms separately" has no single spelling.
