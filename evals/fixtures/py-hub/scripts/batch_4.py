from core.util import Ledger


def run_4(rows):
    ledger = Ledger()
    for cents in rows:
        ledger.post(cents, "batch 4")
    return ledger.balance()
