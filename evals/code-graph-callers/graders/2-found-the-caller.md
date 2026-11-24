---
type: regex
target: "last_message"
pattern: 'replay_order|RevenueReport'
flags: "i"
match: "contains"
---
RevenueReport.replay_order (src/reporting.py:13) is the only caller.
