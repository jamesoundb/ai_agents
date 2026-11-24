---
type: tool_used
tool: Bash
input_match: 'ai-task\s+clone\s+http://localhost:3300/dmtest/design-tokens\.git'
min: 1
---
design-tokens is two levels down (checkout-web -> ui-kit -> design-tokens, package.json:5 in
each). `compass deps checkout-web --kinds build --handoff` prints
`.ai/bin/ai-task clone http://localhost:3300/dmtest/design-tokens.git` (verified 2026-11-24).
The Gitea server is not running in the eval, so the clone fails; the attempt is graded here.
