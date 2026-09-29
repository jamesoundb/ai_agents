---
type: llm
target: "last_message"
criteria: |
  The answer identifies who calls Ledger.post in a Python package that also contains an unrelated
  LegacyLedger.post in billing/legacy.py.

  PASS if the callers reported for Ledger.post are drawn from charge_account (billing/api.py),
  settle (billing/jobs.py) and test_post_records_an_entry (tests/test_core.py), and nothing else.
  Noting that ArchiveExport.run calls a *different* post on LegacyLedger, and is therefore
  excluded, is correct and still passes -- making that distinction is the skill being tested.

  FAIL if ArchiveExport.run or LegacyLedger is listed as a caller of Ledger.post, or if the answer
  treats the two post methods as one symbol, or if it cannot say which callers belong to which.
---
Same judgment call as the Kotlin case, and an `llm` grader for the same reason: an agent that
says "ArchiveExport.run calls LegacyLedger.post, a different class, so it is not in this list"
has demonstrated exactly the resolution under test, and a `not_contains` pattern would mark that
best-case answer wrong for naming what it correctly excluded.
