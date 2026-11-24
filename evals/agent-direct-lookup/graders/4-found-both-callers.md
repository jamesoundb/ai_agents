---
type: regex
target: "last_message"
pattern: 'replay_order|RevenueReport'
flags: "i"
match: "contains"
---
`RevenueReport.replay_order` is the production caller; the test is the other. A fast path is
only worth taking if it still gets the right answer.
