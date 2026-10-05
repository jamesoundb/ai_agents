"""Money helpers."""
from decimal import ROUND_DOWN, Decimal

CENT = Decimal("0.01")


def round_money(amount: Decimal) -> Decimal:
    """Round an amount to whole cents."""
    return amount.quantize(CENT, rounding=ROUND_DOWN)
