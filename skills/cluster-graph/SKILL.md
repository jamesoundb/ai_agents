---
name: cluster-graph
description: >
  Use before kubectl get/describe loops on a live cluster: one read-only snapshot of every resource,
  CRDs included, then one query for what is broken, why, or what depends on an object.
allowed-tools: Bash(*/cluster-graph/scripts/run.sh *), Bash(python3 */cluster-graph/scripts/kubegraph.py *), Bash(kubectl config current-context), Bash(kubectl config get-contexts *), Read
---

# cluster-graph: query a map of the cluster, do not page through kubectl

`scripts/run.sh snapshot` lists every resource type the cluster serves (built-in kinds and custom
resources alike) with read-only `kubectl get -o json` calls in parallel, derives each object's health
from its status and links the objects: ownership, Service and PDB selectors, ConfigMap/Secret/PVC/
ServiceAccount references, Ingress routes, HPA targets, webhooks and APIServices to their Services,
PVC to PV to StorageClass, custom resources to their CRD, and references a missing object as `missing`.
Warning events are attached to the object they are about. One query then replaces a chain of
`get`/`describe` calls: every tool call re-sends the whole conversation, so **fewer calls** matters
more than smaller output.

Engine: `scripts/run.sh` (stdlib Python 3.10+, needs `kubectl` with a working context). Call it by
its full path from the working directory, one command per call: no `cd` into a skill folder, no
shell variable, nothing piped or chained after it. Permission rules match the literal command;
Antigravity denies a chain if any part lacks a grant, and installed skill folders are links outside
the working directory. Read-only by construction: it only runs `get`, `api-resources`, `logs`,
`version` and `config current-context`. Secret values are never stored (key names only).

## Workflow

1. **Pick the context explicitly.** The snapshot is per context (`~/.cache/kubegraph/<context>.db`).
   Check `kubectl config current-context`; pass `--context NAME` on every call when it is not the
   cluster the developer means. Never guess between production and test contexts: ask.
2. **Ask the question in one call.** The first query takes the snapshot (1-5 s on a small cluster, longer
   on a large one); later queries reuse it and re-snapshot by themselves when it is older than 5 minutes
   (`--max-age`), or at once with `--refresh` (after someone applied a fix).

   | You need | One call |
   |---|---|
   | what is broken, cluster-wide | `health` (`-n NS` to narrow) |
   | why X is broken: cause chain, events, logs, next step | `why X` (several objects at once) |
   | requests, limits, usage, owner, children, references | `show X` |
   | what breaks if X changes or is deleted | `used-by X` |
   | what a namespace contains | `tree -n NS` |
   | find an object | `find NAME` (`--kind`, `-n`, `--unhealthy`) |
   | recent events | `events X` or `events -n NS` (`--all` adds Normal) |
   | custom resource types and their health | `crds` |
   | namespaces by requests vs usage | `overview` |
   | what the cluster wastes now (~$/month) | `waste` |
   | can A talk to B (NetworkPolicy, DNS) | `reach A B` (`--port N`) |

   Helm releases are objects too (`Release/NS/NAME`, from Helm's release Secrets: status per revision,
   linked to what they deployed), and Argo CD Applications / Flux HelmReleases and Kustomizations link to
   what they manage, so `why` on any of them reaches the failing workload behind it.
   Objects are written as kind/name, kind/namespace/name, the id a query printed
   (`Deployment/shop/api`), a short name (`deploy/api`, `svc/api`, `te/env-1`) or a bare name
   (a workload wins); `-n` narrows.
3. **Trust the output.** `why` already followed owners, children, failing dependencies and selectors to
   the cause, fetched the failing container's last log lines, listed the existing objects a missing
   reference probably meant (`searched the cluster: no X named Y in any namespace`), named the repository
   file:line that defines each object or that none does (it searched every YAML file of the checkout you
   run it from, or `--repo DIR`) and printed the next read-only command. Do not re-check any of that with
   `kubectl get -A`, `grep` or whole-file reads: before an edit, read only the lines around `defined at`.

## Reading the output

- `FAIL`/`WARN`/`ok`/`-` (no health semantics, e.g. a ConfigMap). Reasons are the cluster's own
  (`CrashLoopBackOff`, `Unschedulable`, `ProvisioningFailed`, a CR's `Ready=False <reason>`) plus a few
  derived ones: `missing`, `missing key`, `NoPods`, `NoReadyEndpoints`, `ProbeFailing`, `Killed`,
  `WebhookUnavailable`, `BackendUnavailable`, `QuotaExhausted`, `Released`.
- `health` groups by top owner (a Deployment, not its ReplicaSet and pods) and lists cluster-scoped
  and kube-system problems first: a broken webhook, APIService or node explains many others.
- `not collected:` names resource types the snapshot could not list (RBAC, an unavailable aggregated
  API); say so when it matters for the answer.
- Quote the lines you rely on (object id, reason, event, log line) as evidence.

Options, the health rules per kind, offline snapshots (`snapshot --from DIR`, `--save DIR`) and limits:
`reference/usage.md`.
