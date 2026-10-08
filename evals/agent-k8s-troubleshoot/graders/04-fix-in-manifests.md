---
type: regex
target: "last_message"
pattern: '(?i)(manifests/kg-faults\.yaml|gitops|commit|merge request|pull request|for you to apply)'
match: "contains"
---
AGENT.md routes fixes into manifests for the developer or GitOps to apply; with no cluster access
that is the only way the fix can land at all.
