# Cluster autoscaler noScaleDown reasons (most common) and what to do

| messageId | meaning | fix |
|---|---|---|
| `no.scale.down.node.pod.not.backed.by.controller` | a bare pod (no Deployment/Job owner) pins the node | delete or give it a controller; `safe-to-evict=true` annotation |
| `no.scale.down.node.pod.kube.system.unmovable` | a kube-system pod without a PDB blocks eviction | add PodDisruptionBudgets for kube-system workloads, or a dedicated small system pool |
| `no.scale.down.node.pod.has.local.storage` | pod uses emptyDir/hostPath | `cluster-autoscaler.kubernetes.io/safe-to-evict: "true"` when the data is disposable (build caches usually are) |
| `no.scale.down.node.no.place.to.move.pods` | remaining nodes cannot fit the pods | smaller node shapes or lower requests; check anti-affinity |
| `no.scale.down.node.minimal.resource.limits.exceeded` | pool at its minimum node count | lower `minNodeCount` (0 for burst-only build pools) |
| `no.scale.down.node.pod.controller.not.found` / `...not.enough.pdb` | PDB or controller constraints | relax PDBs for test workloads |
| `no.scale.down.in.backoff` / `...unremovable.recently` | timing | consider `OPTIMIZE_UTILIZATION` profile |

Source: GKE cluster autoscaler visibility events (`gcloud logging read ... cluster-autoscaler-visibility`).
