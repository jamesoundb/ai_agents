from core.util import Ledger


def run_5(rows):
    ledger = Ledger()
    for cents in rows:
        ledger.post(cents, "batch 5")
    return ledger.balance()
