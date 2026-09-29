"""Reporting."""
from decimal import Decimal

from .orders import OrderService


class RevenueReport:
    def __init__(self, orders: OrderService):
        self.orders = orders

    def replay_order(self, customer_id: str, total: Decimal):
        """Calls through to OrderService so the blast radius has a second hop."""
        return self.orders.place(customer_id, total)

    def daily(self, day):
        return {"day": day, "service": type(self.orders).__name__}
