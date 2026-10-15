---
type: tool_used
tool: Bash
input_match: 'cluster-graph/scripts/(run\.sh|kubegraph\.py)\b'
min: 1
---
AGENT.md step 6: `cluster-graph` `why Release/<namespace>/<release>` or `why HelmRelease/<name>`
gives the Helm revision status (failed upgrade, a pending-operation lock) and the failing objects.
Helm's release state lives in Secrets in the snapshot; nothing else in the workspace shows it.
