package com.acme.shop

/** Submits a basket through the live service. */
class Checkout(private val svc: OrderService) {

    fun submit(orders: List<Order>): List<String> =
        orders.map { svc.place(it) }
}
