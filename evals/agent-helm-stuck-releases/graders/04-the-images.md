---
type: regex
target: "last_message"
pattern: '(?is)(?=.*shop:2\.0)(?=.*cart:2\.0)'
match: "contains"
---
Both revision-2 pods are `ImagePullBackOff` on images that do not exist
(`registry.invalid/team/shop:2.0`, `registry.invalid/team/cart:2.0`; "lookup registry.invalid: no
such host"). kg-shop's failed upgrade does not block the next one, but re-running it with the same
values fails the same way; after kg-cart's rollback, Flux would retry the same bad image.
