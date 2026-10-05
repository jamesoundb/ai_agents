"""Invoices for completed orders."""
from decimal import ROUND_DOWN, Decimal


class Invoice:
    def __init__(self, lines):
        self.lines = lines   # [(unit_price, qty)]

    def total(self) -> Decimal:
        raw = sum((price * qty for price, qty in self.lines), Decimal("0"))
        return raw.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
