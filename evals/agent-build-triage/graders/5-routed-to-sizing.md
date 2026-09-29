---
type: regex
target: "last_message"
pattern: '(?i)(kubernetes agent|memory (request|limit)|gke-cost-discovery|right-?siz|\bp95\b)'
match: "contains"
---
Refusing the rerun without saying what to do instead leaves the developer where they started.
AGENT.md routes by class: `infra/*` goes to the kubernetes agent with the pod details, and the
memory ceiling is raised only against p95 evidence, never by guessing a bigger number.
