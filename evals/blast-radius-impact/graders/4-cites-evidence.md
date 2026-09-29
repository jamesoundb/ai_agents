---
type: llm
target: "last_message"
criteria: |
  The reply must do all of the following:
  1. Distinguish the direct dependent from the transitive one (hop 1 vs hop 2), in
     whatever wording -- "direct/indirect", "hop 1/hop 2", or an ordered chain.
  2. Cite at least one concrete location as file:line (for example
     "src/orders.py:14"), not just a bare filename.
  3. Name tests/test_orders.py as the test to run.
  A reply that lists the right files but gives no line-level citation FAILS.
focus: "hop distinction, file:line citation, tests named"
---
