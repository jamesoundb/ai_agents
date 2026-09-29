---
type: tool_used
tool: Grep
min: 0
max: 0
---
Load-bearing, because on this fixture grep does not merely cost tokens -- **it gets the answer
wrong in both directions**. Verified (2026-09-29):

    grep -rn "Ledger" billing tests   ->  finds the definitions and the test.
                                          billing/api.py and billing/jobs.py, two of the three
                                          real callers, are invisible to it.
    grep -rn "\.post(" billing tests  ->  four hits, one of which (legacy.py:15) calls
                                          LegacyLedger.post, a different class.

So text search under-reports the real callers and over-reports a fake one. An agent that
"verifies empirically" with grep here replaces a correct answer with a wrong one.
