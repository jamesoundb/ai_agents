from core.util import Ledger


def run_2(rows):
    ledger = Ledger()
    for cents in rows:
        ledger.post(cents, "batch 2")
    return ledger.balance()
