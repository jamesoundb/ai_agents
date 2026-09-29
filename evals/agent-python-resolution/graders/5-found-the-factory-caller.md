---
type: regex
target: "last_message"
pattern: 'settle\b'
match: "contains"
---
The second resolution mechanism, deliberately different from the first: inference from a
**return annotation** rather than an import alias.

`billing/jobs.py` calls `open_ledger(book_id)`, whose signature is
`def open_ledger(book_id: str) -> Ledger:`. Nothing in `jobs.py` names the class; the local
`ledger` is typed only by what the factory declares it returns. Verified:

    Ledger.post <- settle  [calls, typed]  (billing/jobs.py:9)

Asserting both this and `charge_account` separately is deliberate -- an answer that finds one
mechanism and misses the other is a partial answer, and which one failed tells you where to look.
