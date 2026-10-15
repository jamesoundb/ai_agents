---
type: regex
target: "last_message"
pattern: '(?is)(?=.*pending[- ]upgrade)(?=.*(\brollback\b|sh\.helm\.release))'
match: "contains"
---
kg-cart's revision 2 is `pending-upgrade`: an interrupted upgrade left Helm's lock, and Flux cannot
upgrade past it. Verified with `why Release/kg-helm/kg-cart` (2026-10-15):

    cause: Release/kg-helm/kg-cart PendingOperation: revision 2 pending-upgrade for 5m: helm refuses every new operation ...; last good revision 1
    advice: ... an operator rolls back to the last good revision (`helm rollback NAME REV`) before any new upgrade can run

The way out is an operator's `helm rollback kg-cart 1` (or removing the pending release Secret),
handed over, not run. Fixing the values alone leaves the lock in place.
