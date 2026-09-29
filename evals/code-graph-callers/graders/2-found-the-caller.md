---
type: regex
target: "last_message"
pattern: '(?i)replay_order|RevenueReport'
match: "contains"
---
RevenueReport.replay_order (src/reporting.py:13) is the only caller.
