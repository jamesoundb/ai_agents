---
type: regex
target: "last_message"
pattern: '(?is)(?=.*janitor/(ttl|expires))(?=.*ttlSecondsAfterFinished)'
match: "contains"
---
The two fixes that belong in `manifests/kg-preview-123.yaml`: a `janitor/ttl` (or `janitor/expires`)
on `preview-app`, as its siblings already carry, and `ttlSecondsAfterFinished` on `seed-data`.
Deleting the disk, PV and load balancer is a cluster action the developer takes; the manifest
changes stop the environment from leaking the same way again.
