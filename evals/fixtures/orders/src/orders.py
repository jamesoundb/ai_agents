"""Order service."""
from decimal import Decimal

from .gateway import PaymentGateway


class OrderService:
    def __init__(self, gateway: PaymentGateway, repo):
        self.gateway = gateway
        self.repo = repo

    def place(self, customer_id: str, total: Decimal) -> dict:
        order = self.repo.create(customer_id, total)
        result = self.gateway.charge(order["id"], total)
        self.repo.mark_paid(order["id"])
        return result

    def cancel(self, order_id: str, amount: Decimal) -> dict:
        return self.gateway.refund(order_id, amount)
