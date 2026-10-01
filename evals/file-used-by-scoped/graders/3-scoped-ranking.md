---
type: llm
target: "last_message"
criteria: |
  The question asks which symbols of core/util.py the code under app/ relies on most, ranked by
  how many files in app/ use each. The correct ranking is: parse_amount (4 app files), then
  format_money (3), then Ledger (1 app file: app/statement.py, with its methods post/balance).
  legacy_round and _cents are not used by app/ at all.

  PASS if parse_amount is ranked first and format_money second, and Ledger is ranked below both
  (as 1 file). Mentioning that Ledger is used heavily by scripts/ outside app/ is correct and
  still passes.

  FAIL if Ledger is ranked first or second, or if counts from scripts/ or tests/ are mixed into
  the app/ ranking (e.g. Ledger with 6 files, parse_amount with 5), or if the ranking is missing.
---
The fixture is built so that the repo-wide ranking and the app/ ranking disagree: repo-wide,
Ledger is used by 6 files (5 batch scripts + app/statement.py) and tops the list; under app/ it
is last. An answer from an unscoped `--used-by` (or an unscoped grep count) gets the order wrong.
Verified against real output: `--used-by --within app` prints parse_amount 4, format_money 3,
Ledger 1; `--used-by --no-tests` prints Ledger 6, parse_amount 4, format_money 3.
