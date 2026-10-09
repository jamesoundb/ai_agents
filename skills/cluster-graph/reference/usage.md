# kubegraph usage reference

## Snapshot

```bash
scripts/run.sh snapshot [--context C] [-n NS ...] [--exclude RES] [--include RES] [--save DIR]
scripts/run.sh snapshot --from DIR [--db PATH]        # offline: saved `kubectl get -o json` lists
```

- One `kubectl api-resources --verbs=list -o wide`, then one `kubectl get <resource> -A -o json` per
  type, 12 in parallel (`--jobs`), each with `--request-timeout` (`--timeout`, default 30 s).
- `-n` limits namespaced types to those namespaces (cluster-scoped types are always listed). Use it on
  very large clusters or where RBAC allows only some namespaces.
- Skipped by default (duplicates or churn): `events.events.k8s.io`, `leases`, `controllerrevisions`,
  `endpoints`, `endpointslices` (Service health is derived from selectors and pod readiness), `componentstatuses`,
  `podtemplates`, `csistoragecapacities`. `--include` brings one back; `--exclude` drops one more
  (e.g. `--exclude secrets` where listing Secrets is not allowed).
- `pods.metrics.k8s.io` / `nodes.metrics.k8s.io` (metrics-server) become current usage on Pods and Nodes.
- Stored in `~/.cache/kubegraph/<context>.db` (`KUBEGRAPH_DIR`, `--db`), written atomically. Queries
  re-snapshot a live snapshot older than `--max-age` (default 300 s, `KUBEGRAPH_MAX_AGE`) with the same
  scope; `--refresh` forces it, `--no-refresh` never does. `--from` snapshots are never refreshed.
- `--save DIR` writes the sanitized lists, `api-resources.json` and `meta.json`: the input for `--from`,
  for sharing a cluster's state with someone who has no access, and for test fixtures.

What is stored: objects without `metadata.managedFields` and the last-applied annotation; Secrets and
ConfigMaps as key names (`dataKeys`) only; env values whose name looks like a credential
(`pass|secret|token|key|cred|auth`) and string values under credential keys (`password`, `token`,
`apiKey`, ...) replaced by `<redacted>`; webhook/APIService `caBundle` and CRD schemas dropped.

## Health rules

| kind | fail | warn |
|---|---|---|
| Pod | waiting reason (CrashLoopBackOff, ImagePullBackOff, CreateContainerConfigError, ...), last termination OOMKilled / exit 137 (`Killed`), Unschedulable, Evicted, Failed, Unknown, not Ready after 2 min (`ProbeFailing` when Unhealthy events exist), ContainerCreating after 2 min (FailedMount ...) | starting (< 2 min), 3+ restarts, stuck Terminating, Unschedulable with a TriggeredScaleUp event |
| Deployment, StatefulSet, ReplicaSet | ReplicaFailure (quota, admission), ProgressDeadlineExceeded, 0 available | fewer ready than desired, rollout in progress, `RolloutStuck` (newest ReplicaSet's pods fail while an older one serves; fail once the progress deadline passed) |
| DaemonSet | 0 ready | fewer ready, misscheduled |
| Job | Failed condition (BackoffLimitExceeded, DeadlineExceeded) | |
| Service | selector matches no pod (`NoPods`), no Ready pod (`NoReadyEndpoints`) | LoadBalancer without an address |
| Ingress | a backend Service missing or failing (`BackendUnavailable`) | |
| HPA | AbleToScale / ScalingActive False | at maxReplicas |
| Node | Ready not True, Memory/Disk/PID pressure, NetworkUnavailable | cordoned |
| PVC / PV | Pending (reason from ProvisioningFailed events), Lost / Failed | PV Released (disk kept and billed) |
| APIService | Available False | |
| Validating/MutatingWebhookConfiguration | backend Service missing or without endpoints, failurePolicy Fail | same with failurePolicy Ignore |
| ResourceQuota | | a resource used up |
| Namespace | | Terminating for over 5 min |
| CRD | Established / NamesAccepted False | |
| custom resources | condition Ready/Available/Healthy/Synced/... False, Degraded/Failed/Stalled True, phase Failed/Error, Argo CD health Degraded/Missing | OutOfSync, Progressing, Reconciling, observedGeneration behind generation |
| referenced object that does not exist | `missing` (unless the reference is `optional`), `missing key` for a configMapKeyRef/secretKeyRef key | |

Health is computed at snapshot time against the snapshot's own clock, so a saved snapshot answers the
same way later.

## Queries

All take `--context`, `--db`, `-n NS`, `--refresh`, `--no-refresh`, `--max-age S`, `--max-rows N`, `--all`.

- `health`: node capacity line (allocatable, requested, used), then unhealthy objects grouped under
  their top owner, cluster-scoped first, then kube-system and other system namespaces, then the rest;
  missing objects list who uses them. Caps at 40 groups (`--max-rows`).
- `why OBJ...`: the object, its owner chain, children (collapsed when identical), dependencies and
  dependents (including Services that select its pods), warning events of the object, its children and
  the PVCs it uses, then `cause:` (a missing dependency first, then the deepest failing dependency, then
  the deepest failing pod), `advice:` for that class, the failing container's last 20 log lines
  (`--previous` when it restarted; `--tail N`, `--no-logs`; live snapshots only) and `next:` commands.
  For a Service whose selector matches nothing it prints the closest pod label sets.
- In a git checkout (or with `--repo DIR`), `why` and `show` print where the repository defines the
  object (`defined at path:line`, pods and ReplicaSets mapped to their top owner), or `not defined in this
  repository`. Plain YAML only (block or flow `metadata`, up to 3,000 files); Helm templates and Kustomize
  name transforms are not resolved. `--no-repo` skips the scan.
- For a `missing` cause, `why` lists the existing objects of that kind with the closest names (same
  namespace first, same name in another namespace flagged, the default StorageClass marked); for
  `missing key`, the keys the ConfigMap/Secret does have. Webhook configurations get one line per
  webhook: operations and resources, failurePolicy, the namespaces its selector matches in the snapshot.
- `show OBJ...`: labels, containers with images and requests/limits, requests x replicas, current usage,
  owner, children, uses, used by, warning events.
- `used-by OBJ...`: reverse dependencies by depth (`--depth`, default 4): users of a ConfigMap, Secret,
  PVC, Service, StorageClass or Node, folded to their top owner, plus Services/PDBs that select the pods.
- `tree OBJ | -n NS`: ownership trees with health; old ReplicaSets hidden (`--all`).
- `find [PATTERN]`: substring, or a glob with `*?[`; `--kind`, `-n`, `--unhealthy`.
- `events [OBJ] [-n NS]`: grouped events, newest first; warnings only unless `--all`.
- `crds`: every CRD with its instance count and unhealthy instances.
- `overview`: per namespace running/total pods, workloads, requests vs usage (cpu, memory), unhealthy.
- `waste`: what the snapshot shows the cluster paying for without use, in ~$/month at the list prices of
  `../gke-cost-discovery/reference/pricing.json`: requests reserved by scheduled pods that fail; pods
  requesting far more than they use now (one sample: a lead for `gke-cost-discovery`, not a size);
  Bound PVCs no pod mounts, Released/Available PVs (priced by the StorageClass's PD type); LoadBalancer
  Services; finished Jobs without `ttlSecondsAfterFinished`; Deployments/StatefulSets in test namespaces
  (`--env-ns`, default `*preview*,*review*,*test*,pr-*,*-pr-*,*ephemeral*,*sandbox*`) older than
  `--min-age` hours (4) without `janitor/ttl`/`janitor/expires` on them or their namespace; scale-down
  blockers (safe-to-evict=false, bare pods, local storage, PDBs allowing 0 disruptions); nodes under 40%
  requested.
- `reach A B [--port N] [--protocol P]`: NetworkPolicy evaluation for traffic from A (pod, workload) to B
  (pod, workload or Service; a Service's targetPort, named ports resolved, is the default port): egress
  policies selecting A, ingress policies selecting B, each with the rule that allows it or the rules that
  exist when it is denied; whether A's egress policies allow DNS to kube-dns (53/UDP); and whether a
  policy-enforcing CNI runs in kube-system (without one, policies are not enforced at all).

## Limits

- A snapshot is a point in time: re-run with `--refresh` after a change, and treat states younger than
  2 minutes as transient.
- No exec, no port-forward, no network probes: connectivity is evaluated from Service endpoints and
  NetworkPolicies (`reach`), not tested; ipBlock peers are matched against pod IPs only.
- Logs are only fetched by `why`, never stored.
- Custom resources get generic health (conditions, phase, Argo CD health/sync, observedGeneration) and
  generic references (`secretName`, `serviceName`, `*Ref.name` to objects that exist).
- Large clusters: the snapshot holds every object's sanitized JSON (roughly 5-10 KB per pod). Use `-n`
  when only some namespaces matter.
