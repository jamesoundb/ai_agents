from core.util import Ledger


def run_3(rows):
    ledger = Ledger()
    for cents in rows:
        ledger.post(cents, "batch 3")
    return ledger.balance()
