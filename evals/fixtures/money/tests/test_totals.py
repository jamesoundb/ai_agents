from decimal import Decimal

from src.cart import Cart
from src.invoice import Invoice


def test_cart_whole_cents():
    cart = Cart()
    cart.add(Decimal("2.50"), 2)
    assert cart.total() == Decimal("5.00")


def test_invoice_whole_cents():
    assert Invoice([(Decimal("2.50"), 2)]).total() == Decimal("5.00")
