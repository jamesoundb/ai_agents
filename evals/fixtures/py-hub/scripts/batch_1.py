from core.util import Ledger


def run_1(rows):
    ledger = Ledger()
    for cents in rows:
        ledger.post(cents, "batch 1")
    return ledger.balance()
