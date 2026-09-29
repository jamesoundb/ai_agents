package com.acme.shop

/** The live order path. */
class OrderService(private val repo: OrderRepo) {

    fun place(order: Order): String {
        val priced = order.withTax(0.2)
        repo.save(priced)
        return priced.id
    }
}

class OrderRepo {
    fun save(order: Order) {
        println("saved ${order.id}")
    }
}
