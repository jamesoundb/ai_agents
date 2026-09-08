# k8s-manifest-review rules

| id | sev | rule |
|---|---|---|
| COR000 | high/medium | YAML parse error / Helm template syntax (render first) |
| COR001 | medium | workload without metadata.namespace |
| COR002 | low | missing required labels |
| COR003 | low | Deployment container without readinessProbe (non-build only) |
| COR004 | high | privileged container, hostNetwork/hostPID, hostPath volume |
| COR005 | low | pinned image pulled with imagePullPolicy Always |
| COR006 | low | CronJob concurrencyPolicy Allow |
| EFF001 | high | container without cpu/memory requests |
| EFF002 | medium | container without memory limit |
| EFF003 | low/info | memory limit differs from request / cpu limit >= 8x request |
| EFF004 | high/info | pod requests exceed its tier / no tier label (evaluated against default tier) |
| EFF005 | high/medium | Job (high) or CronJob template (medium) without ttlSecondsAfterFinished |
| EFF006 | medium | Job without activeDeadlineSeconds |
| EFF007 | low | Job backoffLimit > 3 |
| EFF008 | high | build-namespace Deployment/StatefulSet without a kube-janitor TTL (`janitor/ttl` or `janitor/expires`) |
| EFF009 | medium | test Deployment replicas above `max_test_replicas` |
| EFF010 | medium | build workload not steered to the spot pool |
| EFF011 | low | PVC in a build/test workload |
| EFF012 | low | untagged or :latest image |
| EFF013 | low/info | no ephemeral-storage request / emptyDir without sizeLimit |
| EFF015 | low | more than `max_containers` containers in a build pod |

Severity is adjustable per repo with `severity_overrides`; rules can be switched off with
`disabled_rules`. Tiers come from `build-tiers.json` (gke-cost-discovery) or inline `tiers`.
