---
name: k8s-guardrails
description: >
  Generate namespace guardrails for build and test namespaces from the company's build size
  tiers: a LimitRange (defaults and per-container maximums, memory limit = request), a
  ResourceQuota sized for expected concurrency, and a kube-janitor rules entry that gives
  unannotated test environments a default TTL (a standalone janitor CronJob is available only
  for clusters without kube-janitor). Use after gke-cost-discovery has produced
  build-tiers.json, when a namespace has no LimitRange, or when test environments are left
  running.
allowed-tools: Bash(python3 */k8s-guardrails/scripts/guardrails.py *), Bash(*/k8s-guardrails/scripts/guardrails.py *), Bash(kubeconform *), Read, Write, Glob
---

# k8s-guardrails: make the tier table enforceable

Manifests get reviewed; admission gets enforced. `scripts/guardrails.py` renders the templates
in `templates/` (edit them to change company defaults) into per-namespace YAML that GitOps can
apply. This skill never applies anything itself.

```bash
scripts/guardrails.py --tiers build-tiers.json --namespaces teamcity-agents,test-envs --out ./guardrails \
                      [--concurrent-pods 20] [--ttl-hours 8] [--no-kube-janitor-rule] [--standalone-janitor] [--tier-label build.company.io/tier]
```

What is generated per namespace:

| file | content |
|---|---|
| `<ns>-limitrange.yaml` | `defaultRequest` = small tier, `default` limit cpu = medium / memory = small (limit = request), `max` = large tier, `maxLimitRequestRatio` cpu 4 / memory 1, pod max = large |
| `<ns>-resourcequota.yaml` | `requests.cpu/memory` and `limits.memory` = medium tier x `--concurrent-pods`, `pods`, `count/jobs.batch`, `persistentvolumeclaims: "0"` |
| `<ns>-kube-janitor-rule.yaml` (default) | rules-file entry for the company's kube-janitor: Deployments/StatefulSets/Jobs in the namespace without `janitor/ttl` or `janitor/expires` expire after `--ttl-hours`; annotations always win, `janitor/ttl: forever` opts out. Merge into kube-janitor's rules ConfigMap |
| `<ns>-janitor.yaml` (`--standalone-janitor`) | self-contained CronJob + RBAC for clusters WITHOUT kube-janitor; never run both |

## Procedure

1. Take `build-tiers.json` from the discovery report (or agree tiers with the team and write the
   file by hand in the same shape). Choose `--concurrent-pods` from the report's TeamCity peak
   concurrency, with some margin.
2. Generate, then review the output with `k8s-manifest-review` (it must pass) and validate the
   schema with `kubeconform` when available.
3. Explain the consequences before adoption: pods without requests now get the small tier by
   default; pods above the large tier are rejected at admission; the quota caps total build
   capacity per namespace; the kube-janitor rule expires unannotated test environments after
   the TTL. Recommend rolling out LimitRange first, quota second, the janitor rule last (after a
   `--dry-run` pass of kube-janitor), one namespace at a time.
4. Hand the files to the GitOps repository (ArgoCD/Flux) rather than applying them. Before
   relying on the rule, check with `gke-cost-discovery` that kube-janitor is deployed, not in
   `--dry-run`, and includes the namespace and the `deployments` resource.
