"""Decommissioned ledger kept for the archive export. Same method name, different class."""
from decimal import Decimal


class LegacyLedger:
    def post(self, amount: Decimal, memo: str) -> dict:
        return {"legacy": True, "amount": amount, "memo": memo}


class ArchiveExport:
    def __init__(self):
        self.ledger = LegacyLedger()

    def run(self, amount: Decimal) -> dict:
        return self.ledger.post(amount, "archive")
