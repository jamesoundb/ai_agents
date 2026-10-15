---
type: regex
target: "last_message"
pattern: '(?is)(?=.*\bdns\b)(?=.*\b53\b)'
match: "contains"
---
The trap. `api -> db` is allowed on 5432 by both policies, but `api-egress` selects api for Egress
with no rule for kube-dns, so `db` never resolves. Verified on this snapshot (2026-10-15):

    dns from source (kube-dns 53/UDP): DENIED: the source's egress policies have no rule for kube-dns, so name lookups fail before any connection

An answer that says "api -> db: allowed" has read the ports, not the path. The fix is an egress rule
to kube-dns on 53 (UDP and TCP).
