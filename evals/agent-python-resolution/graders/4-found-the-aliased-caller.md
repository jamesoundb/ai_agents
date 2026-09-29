---
type: regex
target: "last_message"
pattern: 'charge_account'
match: "contains"
---
The re-export case, and the one a Python developer meets constantly.

`billing/__init__.py` does `from .core import Ledger as Book`; `billing/api.py` imports `Book`
and calls `book.post(...)`. The defining name never appears in the calling file. Verified graph
output:

    Ledger.post <- charge_account  [calls, typed]  (billing/api.py:10)

The linker followed the alias through the package's public surface. No text search for `Ledger`
reaches this line.
