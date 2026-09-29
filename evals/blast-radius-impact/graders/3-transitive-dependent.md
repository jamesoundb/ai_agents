---
type: regex
target: "last_message"
pattern: '(?i)reporting\.py'
match: "contains"
---
Hop 2: RevenueReport.replay_order -> OrderService.place -> PaymentGateway.charge.

This is the grader that separates a real graph traversal from a single-file grep.
A grep for "charge" never reaches reporting.py, because reporting.py does not
mention charge at all.
