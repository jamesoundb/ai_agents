# helm-chart-review rules

| id | sev | rule |
|---|---|---|
| HC000 | high | Chart.yaml missing |
| HC001 | medium | apiVersion not v2 |
| HC002 | medium | chart version not semver |
| HC003 | low | no appVersion |
| HC004 | high/medium | dependency version missing (high) or a range (medium); dependencies without Chart.lock |
| HC005 | low | no values.schema.json |
| HC006 | low | no README.md |
| HC007 | info | no templates/NOTES.txt |
| HV001 | medium | default image tag latest |
| HV002 | low | default pullPolicy Always |
| HV003 | medium | no resources.requests defaults in values.yaml |
| HV004 | critical | secret-looking value in values files, inline Application/HelmRelease values |
| HV005 | low | value not referenced by any template (dead value) |
| HV006 | medium | replicaCount above `max_test_replicas` in a test-like values file |
| HV007 | low | requests without memory limit in a values file |
| HG000 | high | GitOps YAML parse error |
| HG001 | high | ArgoCD targetRevision is HEAD/main/master or unset; Flux chart version unpinned |
| HG002 | high | ArgoCD valueFiles entry that does not exist in the chart |
| HG003 | medium | ArgoCD replicaCount parameter above the test cap in a test namespace |
| HG004 | medium | ArgoCD project default or not in `allowed_projects` |
| HG005 | low/medium | prune without selfHeal (low); test namespace without prune (medium) |
| HG006 | medium | no destination.namespace |
| HG007 | high | test-environment Application/HelmRelease without `janitor/ttl` |
| HG008 | low | Flux interval under 5m |
| HG009 | medium | Flux remediation retries -1 |
| HL001 | high/low | helm lint error / warning |
| HR-<rule> | as reported | k8s-manifest-review finding on rendered manifests (medium and above) |
