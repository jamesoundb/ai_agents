---
type: llm
target: "trace"
criteria: |
  The report names two failing cases (the cart total and the invoice total for 3 x 0.335, both
  expected 1.01) and one constraint (whole-cent totals such as 5.00 unchanged). Cart.total rounds
  through round_money in src/money.py; Invoice.total in src/invoice.py has its own quantize call,
  so the defect is in two places.

  PASS if, by the end of the trace, BOTH places round half up to the cent (ROUND_HALF_UP or an
  equivalent), tests were added for the cart case and the invoice case asserting 1.01, and the
  final message accounts for each of the three items.

  FAIL if only one of the two places is fixed, if no test asserts the invoice result, or if the
  final message claims the fix is complete while a named case is untested.
---
The fixture's second site is the point: fixing the shared helper fixes the cart and leaves the
invoice wrong, which is the "first variant only" failure the skill's checklist exists to catch.
