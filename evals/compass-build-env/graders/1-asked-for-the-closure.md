---
type: tool_used
tool: Bash
input_match: 'compass/scripts/run\.sh\b.*\bdeps\b'
min: 1
---
An environment needs every repository at any depth: `compass deps checkout-web`, not `plan`
(which proposes the repositories a change touches) or `show` (direct dependencies only).
