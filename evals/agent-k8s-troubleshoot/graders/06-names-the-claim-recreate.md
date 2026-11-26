---
type: regex
target: "last_message"
pattern: '(?=.*data-pending)(?=.*(re-?creat|delete[^\n]{0,80}(claim|pvc)|(claim|pvc)[^\n]{0,80}delete))'
flags: "is"
match: "contains"
---
A claim's spec is fixed at creation: a corrected `storageClassName` in the manifest does not apply over
the existing Pending `data-pending` claim (server dry-run on minikube 2026-11-26: "spec is immutable
after creation except resources.requests for bound claims"; `--dry-run=client` reported "configured").
`why` prints it as `immutable:`, and the report must carry the operator step: delete the claim (never
bound, no data) and let GitOps recreate it. A live run on 2026-11-26 changed the class and left it out.
