---
name: k8s-cluster-triage
description: >
  Use when a cluster or namespace misbehaves broadly (many pods failing, deploys rejected, nodes
  NotReady, HPAs blind): a live health sweep, shared causes first, each finding with evidence.
allowed-tools: Bash(*/cluster-graph/scripts/run.sh *), Bash(*/k8s-cluster-triage/../cluster-graph/scripts/run.sh *), Bash(kubectl config current-context), Bash(kubectl get *), Bash(kubectl describe *), Bash(kubectl logs *), Bash(kubectl top *), Read
---

# k8s-cluster-triage: one sweep, shared causes first

When many things fail at once, they rarely have many causes. Find the shared one before fixing
symptoms one by one. Engine: the `cluster-graph` skill's `scripts/run.sh`, installed side by side
(for example `.claude/skills/cluster-graph/scripts/run.sh` or
`.agents/skills/cluster-graph/scripts/run.sh`; written `../cluster-graph/scripts/run.sh` below).
Call it by that path from the working directory, one command per call: no `cd` into a skill folder,
no shell variable, nothing piped or chained after it. Permission rules match the literal command;
Antigravity denies a chain if any part lacks a grant, and installed skill folders are links outside
the working directory. Read-only: fixes go into manifests, Helm values or Terraform; operational
steps (cordon, restart, delete) are named for an operator, never run.

## Procedure

1. **Context.** `kubectl config current-context`; if it is not clearly the cluster the developer means,
   ask. Pass `--context NAME` on every engine call.
2. **Sweep in one call:** `../cluster-graph/scripts/run.sh health --context NAME` (`-n NS` for a
   namespace). Add `--refresh` if a fix was applied since the last snapshot.
3. **Read it top-down; the order is the triage order:**
   - `not collected:` an unavailable API group (APIService) also breaks `kubectl` discovery, HPAs and
     `kubectl top`; RBAC gaps mean the sweep is partial: say which.
   - cluster-scoped: Nodes (NotReady, pressure, cordoned), admission webhooks with failurePolicy Fail
     and no backend (they reject deploys everywhere they match), APIServices, CRDs, Released PVs.
   - kube-system and other system namespaces: CoreDNS (all name resolution), CNI/kube-proxy
     (networking), metrics-server (HPAs, `kubectl top`), CSI drivers (volumes), the ingress controller.
   - the capacity line: requested close to allocatable means Unschedulable pods are a capacity or
     over-requesting problem, not a scheduler bug.
   - workload groups: look for one reason repeated across workloads (table below).
4. **Explain the top groups in one call:** `why A B C` with the first object of each of the top 1-3
   groups. `why` prints the cause chain, events, the failing container's logs and the next command;
   do not repeat it with `describe`.
5. **Cost side, when asked or when capacity is the symptom:** `waste` in one call (idle requests,
   unmounted disks, load balancers, environments without a TTL, scale-down blockers, underfilled nodes).
6. **Report**, ranked by blast radius (cluster-wide, then namespace, then single workload):

   | # | severity | object | reason | evidence (quoted) | cause | fix and owner |
   |---|---|---|---|---|---|---|

   then "Not collected", "Evidence" (the commands) and "Limits" (snapshot age, what a snapshot
   cannot see: in-pod DNS/connectivity, which needs an operator with exec rights).

## Shared causes

| pattern in `health` | likely shared cause | check with |
|---|---|---|
| many pods `ImagePullBackOff` from one registry | registry outage, expired pull credentials, egress | `why` one pod: the event message names it |
| many `Unschedulable` with `Insufficient cpu/memory` | autoscaler at max, node pool too small, or requests far above usage | capacity line; `overview` requests vs used; kubernetes agent cost procedure |
| `Unschedulable` with `node selector`/`untolerated taint` | node pool labels or taints changed | `show Node/X` |
| `ReplicaFailure` with `admission webhook ... failed calling` | webhook backend down | the `WebhookUnavailable` line, `used-by Service/...` |
| `ReplicaFailure` with `exceeded quota` | ResourceQuota used up | the `QuotaExhausted` line |
| HPAs `FailedGetResourceMetric` in several namespaces | metrics-server / `v1beta1.metrics.k8s.io` down (or no CPU requests) | `why APIService/v1beta1.metrics.k8s.io` |
| failing pods all on one node | the node (pressure, NotReady, disk) | `used-by Node/X` |
| one custom resource kind `Ready=False` everywhere | its controller (operator) is down or lacks RBAC | `find <operator> --kind pod`, `why` it |
| several PVCs `Pending` | StorageClass, provisioner or CSI driver | `why` one PVC, `crds` / kube-system pods |
| a namespace stuck `Terminating` | a finalizer whose controller is gone, or an unavailable API group | the namespace's condition message |
