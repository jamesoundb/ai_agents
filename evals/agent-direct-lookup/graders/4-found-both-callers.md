---
type: regex
target: "last_message"
pattern: '(?i)replay_order|RevenueReport'
match: "contains"
---
`RevenueReport.replay_order` is the production caller; the test is the other. A fast path is
only worth taking if it still gets the right answer.
