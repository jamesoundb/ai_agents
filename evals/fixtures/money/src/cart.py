"""Shopping cart."""
from decimal import Decimal

from .money import round_money


class Cart:
    def __init__(self):
        self.lines = []

    def add(self, unit_price: Decimal, qty: int) -> None:
        self.lines.append((unit_price, qty))

    def total(self) -> Decimal:
        return round_money(sum((price * qty for price, qty in self.lines), Decimal("0")))
