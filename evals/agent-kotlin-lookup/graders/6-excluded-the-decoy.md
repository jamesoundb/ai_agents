---
type: llm
target: "last_message"
criteria: |
  The answer identifies who calls OrderService.place in a Kotlin codebase that also contains an
  unrelated LegacyOrderService.place.

  PASS if the callers it reports for OrderService.place are Checkout.submit and/or the test
  OrderServiceTest.placesAnOrder, and nothing else. Explicitly noting that
  LegacyOrderService.place or LegacyBatch.run is a *different* symbol, excluded from the answer,
  is correct and still passes -- distinguishing them is the skill being tested.

  FAIL if LegacyBatch.run or LegacyOrderService is listed as a caller of OrderService.place, or
  if the answer treats the two place methods as one symbol, or if it hedges about which callers
  belong to which without resolving it.
---
The one judgment call in this case, and the reason it is an `llm` grader rather than a regex.

A `not_contains: LegacyBatch` pattern would fail the *best* answers -- an agent that says "note:
LegacyBatch.run calls a different `place` on LegacyOrderService, so it is not in this list" has
demonstrated exactly the resolution this fixture tests, and would be marked wrong for mentioning
the thing it correctly excluded. The distinction is between listing the decoy and excluding it
out loud, which needs a reader.

Criteria are written with explicit PASS and FAIL conditions because vague criteria are the main
source of judge noise (see this README's grader guidance).
