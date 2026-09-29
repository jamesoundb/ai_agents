"""Public surface. `Book` is the exported name for the internal `Ledger`."""
from .core import Ledger as Book
from .core import open_ledger

__all__ = ["Book", "open_ledger"]
