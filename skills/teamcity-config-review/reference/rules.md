# teamcity-config-review rules

| id | sev | rule |
|---|---|---|
| TC000 | info | no BuildType/Project/cloud image blocks recognised in a Kotlin file |
| TC001 | high/low | no executionTimeoutMin (high); above policy max (low) |
| TC002 | medium | project without cleanup rules |
| TC003 | medium/low | VCS trigger without branchFilter (medium) / without quiet period (low) |
| TC004 | low | docker build without layer cache; gradle without build cache |
| TC005-<rule> | as reported | k8s-manifest-review finding on an embedded pod template (medium and above) |
| TC006 | info | no agent requirements (build may land on any pool) |
| TC007 | low | no maxRunningBuilds cap on a container/Kubernetes build |
| TC008 | medium | artifactRules publish everything (`+:**`) |
| TC009 | critical | plain-text secret in a parameter |
| TC010 | low | step image untagged or :latest |
| TC012 | high | build creates/scales Kubernetes resources with no teardown step |
| TC013 | medium | build scales a test deployment above 2 replicas |
