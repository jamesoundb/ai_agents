"""The live ledger."""
from decimal import Decimal


class Ledger:
    """Records postings for the current book."""

    def __init__(self, book_id: str):
        self.book_id = book_id
        self.entries: list[dict] = []

    def post(self, amount: Decimal, memo: str) -> dict:
        entry = {"book": self.book_id, "amount": amount, "memo": memo}
        self.entries.append(entry)
        return entry


def open_ledger(book_id: str) -> Ledger:
    """Factory: the return annotation is the only thing that types the caller's local."""
    return Ledger(book_id)
