---
name: k8s-workload-triage
description: >
  Use when one workload, pod, Job, Service or custom resource fails, restarts or is unreachable:
  the cause from the live cluster with quoted evidence and logs, and the fix as a manifest change.
allowed-tools: Bash(*/cluster-graph/scripts/run.sh *), Bash(*/k8s-workload-triage/../cluster-graph/scripts/run.sh *), Bash(kubectl config current-context), Bash(kubectl get *), Bash(kubectl describe *), Bash(kubectl logs *), Bash(kubectl apply --dry-run=*), Read, Edit, Glob, Grep
---

# k8s-workload-triage: cause, evidence, fix in the manifest

Engine: `../cluster-graph/scripts/run.sh`, relative to this skill folder. Read-only against the
cluster: the fix goes into the manifest or Helm values for the developer or GitOps to apply.

## Procedure

1. **Context.** `kubectl config current-context`; ask when it is not clearly the right cluster, then pass
   `--context NAME` on every call.
2. **One call:** `../cluster-graph/scripts/run.sh why KIND/NAME -n NS --context NAME` (several objects
   at once if the report names several). It prints the owner chain, children, failing dependencies,
   the Services that select the pods, warning events, `cause:`, `advice:`, the failing container's last
   log lines and `next:`. A bare name works; the error lists candidates when it is ambiguous.
3. **Act on the cause class**, not the symptom (a Deployment `Unavailable` is a symptom):

   | cause | the fix belongs in | note |
   |---|---|---|
   | `CrashLoopBackOff`, `Error` | app config, command/args, a dependency it needs at startup | quote the log line that names it |
   | `OOMKilled`, `Killed` (137) | `resources.limits.memory` | `show` gives current usage; with a discovery report, size from its peak (`k8s-rightsize`) |
   | `ImagePullBackOff`, `ErrImagePull` | image name/tag, `imagePullSecrets` | the event names registry and error |
   | `CreateContainerConfigError`, `missing`, `missing key` | create the ConfigMap/Secret through GitOps, or fix the name/key | `used-by` shows who else needs it |
   | `Unschedulable` | requests vs free capacity, nodeSelector/affinity, tolerations | the message lists each node's reason; do not raise node pools before checking over-requesting |
   | `ProbeFailing`, `NotReady` | probe path/port, `initialDelaySeconds`, a `startupProbe` | the event names the probe and the error |
   | `NoPods` | Service selector or pod labels | `why` prints the closest pod labels |
   | `NoReadyEndpoints`, `BackendUnavailable` | the pods behind it | follow `cause:` |
   | PVC `Pending`, `ProvisioningFailed` | `storageClassName` | `cause:` follows pod -> PVC -> StorageClass |
   | `BackoffLimitExceeded`, `DeadlineExceeded` | the job's command, data or `activeDeadlineSeconds` | logs of the last pod |
   | `FailedGetResourceMetric` (HPA) | CPU/memory requests on the target, or metrics-server | |
   | `ReplicaFailure` | quota, LimitRange, PodSecurity, a webhook | the message says which |
   | custom resource `Ready=False ...` | what the controller's message says | the controller's own logs: `find <operator> --kind pod` |

4. **Fix in the repo.** Locate the manifest or values file (`Grep` for the object name), edit it, and
   validate with `kubectl apply --dry-run=client -f FILE` (and `--dry-run=server` when the cluster is
   reachable); build and test manifests also go through `k8s-manifest-review`.
5. **Verify after the change is applied** (by the developer or GitOps): `why KIND/NAME --refresh`. A new
   cause can appear once the first is fixed (a pod that now starts may crash); repeat from step 3.

## Output shape

`cause:` line and class first, the evidence quoted (status reason, event, log line), the fix as a diff
with the file it belongs in, the verification command, then "Evidence" and "Limits" (snapshot age;
what was not collected).
