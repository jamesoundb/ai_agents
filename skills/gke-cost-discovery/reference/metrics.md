# Evidence sources used by collect.sh

| file | source | what it gives |
|---|---|---|
| `cluster.json` | `gcloud container clusters describe` | node pools: machine type, spot, autoscaling min/max, zones; autoscaling profile; VPA flag |
| `nodes.json`, `pods.json`, `workloads.json`, `pdb.json`, `quotas.json`, `events.json` | `kubectl get ... -o json` | point-in-time requests vs allocatable per node, pods without requests, limit/request ratios, lingering pods, stale Deployments, Jobs without TTL/deadline, scale-down blockers, LimitRange coverage |
| `top.txt` | `kubectl top pods --containers` | instantaneous usage (metrics-server) |
| `ts_cpu_req_util.json`, `ts_mem_req_util.json` | Cloud Monitoring `kubernetes.io/container/cpu/request_utilization` (hourly ALIGN_PERCENTILE_95) and `.../memory/request_utilization` (hourly ALIGN_MAX: memory is sized from its peak because exceeding it is an OOM kill, while CPU above p95 is merely throttled), grouped by namespace, container and top-level controller | usage as a fraction of requests: the "unused" bucket |
| `ts_cpu_req_cores.json`, `ts_mem_req_bytes.json` | `kubernetes.io/container/cpu/request_cores`, `.../memory/request_bytes` | what was requested, per workload container, hourly |
| `ts_node_req_cores.json`, `ts_node_alloc_cores.json` | container request_cores summed per node; `kubernetes.io/node/cpu/allocatable_cores` | allocatable minus requested over time: the "unallocated" bucket |
| `ts_node_cpu_util.json` | `kubernetes.io/node/cpu/allocatable_utilization` | actual node busyness (sanity check) |
| `autoscaler_logs.json` | Cloud Logging `cluster-autoscaler-visibility` | scale-up/down decisions and noScaleDown reasons |
| `teamcity_builds.json`, `teamcity_buildtypes.json`, `teamcity_cloud.json` | TeamCity REST `/app/rest/builds`, `/buildTypes`, `/cloud/profiles` | demand per build configuration, concurrency, queue time; which configurations run kubectl/helm; cloud profile images (pod templates) |

Cloud Monitoring is queried through the REST API `projects.timeSeries.list` with an hourly
alignment period; the collector pages through results. Window: `DISCOVERY_DAYS` (default 14).
Autopilot clusters bill per pod request; on Standard the same metrics apply but cost follows
nodes, which is why the unallocated bucket exists.

## Optional: real dollars per namespace (`billing.csv`)

The report ranks namespaces by list-price estimates from requests. To rank by actual cost, enable
GKE cost allocation on the cluster (`gcloud container clusters update CLUSTER --enable-cost-allocation`)
and export billing to BigQuery; then drop a `billing.csv` with columns `namespace,cost_month`
into the discovery directory. Query (last 30 days, namespace label added by cost allocation):

```sql
SELECT l.value AS namespace, ROUND(SUM(cost), 2) AS cost_month
FROM `PROJECT.DATASET.gcp_billing_export_resource_v1_XXXX`, UNNEST(labels) AS l
WHERE l.key = 'k8s-namespace'
  AND service.description = 'Kubernetes Engine'
  AND usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
GROUP BY namespace ORDER BY cost_month DESC
```

Unallocated node cost appears in the export without a namespace label; the report's
"unallocated" bucket explains that remainder.
