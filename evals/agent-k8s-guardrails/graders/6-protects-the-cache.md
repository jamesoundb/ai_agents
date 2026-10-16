---
type: regex
target: "last_message"
pattern: '(?is)gradle-cache.{0,500}(janitor|expire|ttl|deleted|forever|persistentvolumeclaim|pvc)'
match: "contains"
---
`gradle-cache.yaml` is the namespace's one long-lived object: a Deployment without `janitor/ttl`
(the default rule expires it after 8h) and a PVC (the quota's `persistentvolumeclaims: "0"`
blocks re-creating it). Either consequence named for the cache passes.
