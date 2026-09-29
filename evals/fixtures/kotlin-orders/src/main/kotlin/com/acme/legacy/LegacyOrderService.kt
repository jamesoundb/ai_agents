package com.acme.legacy

import com.acme.shop.Order

/** Decommissioned path kept for the nightly batch. Same method name, different class. */
class LegacyOrderService {
    fun place(order: Order): String = order.id
}

class LegacyBatch(private val svc: LegacyOrderService) {
    fun run(orders: List<Order>): List<String> = orders.map { svc.place(it) }
}
