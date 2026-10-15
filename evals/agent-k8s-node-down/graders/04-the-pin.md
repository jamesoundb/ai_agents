---
type: regex
target: "last_message"
pattern: '(?i)(nodeSelector|kubernetes\.io/hostname)'
match: "contains"
---
The part that is ours to fix: `manifests/kg-faults.yaml` pins `on-m02` with
`nodeSelector: {kubernetes.io/hostname: agents-test-m02}`, so both replicas live on one node and no
replacement can schedule anywhere else while it is down. The node explains today's outage; the pin is
why one node took the whole Deployment with it.
