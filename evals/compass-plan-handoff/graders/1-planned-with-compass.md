---
type: tool_used
tool: Bash
input_match: 'compass/scripts/(run\.sh|compass\.py)\s+plan\b'
min: 1
---
The map is already built (scaffold), so the first useful call is `compass plan` with the problem's
words. Verified 2026-11-23: it proposes dmtest/payments-api and dmtest/checkout-web and prints the
`ai-task clone` commands.
