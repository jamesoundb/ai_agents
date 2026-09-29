"""Nightly jobs. Reaches the ledger through a factory's return annotation."""
from decimal import Decimal

from .core import open_ledger


def settle(book_id: str) -> dict:
    ledger = open_ledger(book_id)
    return ledger.post(Decimal("0.00"), "settlement")
