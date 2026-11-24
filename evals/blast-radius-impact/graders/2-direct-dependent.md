---
type: regex
target: "last_message"
pattern: 'orders\.py'
flags: "i"
match: "contains"
---
Hop 1: OrderService.place calls PaymentGateway.charge.
