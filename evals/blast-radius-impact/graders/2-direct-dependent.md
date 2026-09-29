---
type: regex
target: "last_message"
pattern: '(?i)orders\.py'
match: "contains"
---
Hop 1: OrderService.place calls PaymentGateway.charge.
