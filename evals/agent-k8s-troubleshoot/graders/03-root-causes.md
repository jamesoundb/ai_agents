---
type: regex
target: "last_message"
pattern: '(?is)(?=.*does-not-exist)(?=.*app-settings)(?=.*shop-frontend)'
match: "contains"
---
All three root causes, not the symptoms. Verified against `why` on this snapshot (2026-10-08):

    cause: StorageClass/does-not-exist missing ... (via PersistentVolumeClaim/kg-faults/data-pending, storageClassName)
    cause: ConfigMap/kg-faults/app-settings missing ... (via Deployment/kg-faults/missing-config, env LOG_LEVEL key log_level (app))
    cause: Service/kg-faults/shop-frontend missing ... (via Ingress/kg-faults/shop)

"uses-pvc is Pending" or "the Deployment is unavailable" are symptoms; an answer that stops there
has not found the cause.
