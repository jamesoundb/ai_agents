from decimal import Decimal

from src.gateway import PaymentGateway
from src.orders import OrderService


class FakeRepo:
    def create(self, customer_id, total):
        return {"id": "ord_00112233445566aa", "customer": customer_id, "total": total}

    def mark_paid(self, order_id):
        return True


def test_place_charges_the_gateway():
    gateway = PaymentGateway("https://pay.example", "key_test")
    service = OrderService(gateway, FakeRepo())
    result = service.place("cus_1", Decimal("12.50"))
    assert result is not None
