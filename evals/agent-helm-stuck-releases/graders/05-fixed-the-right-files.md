---
type: regex
target: "last_message"
pattern: '(?is)(?=.*releases/kg-shop\.yaml)(?=.*cart-helmrelease\.yaml)'
match: "contains"
---
Each image comes from a different layer: kg-shop's from the CI values file, kg-cart's from the Flux
HelmRelease's inline values. The chart default (`busybox:1.36`, revision 1) is fine; changing it
fixes neither release.
