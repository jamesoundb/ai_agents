"""Request handlers. Reaches the ledger through the re-exported alias."""
from decimal import Decimal

from . import Book


def charge_account(book_id: str, amount: Decimal) -> dict:
    """The class is bound here under its exported alias, not its defining name."""
    book = Book(book_id)
    return book.post(amount, "charge")
