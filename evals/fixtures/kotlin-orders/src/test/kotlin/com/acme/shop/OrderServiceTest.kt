package com.acme.shop

import kotlin.test.Test
import kotlin.test.assertEquals

class OrderServiceTest {

    @Test
    fun placesAnOrder() {
        val svc = OrderService(OrderRepo())
        assertEquals("a", svc.place(Order("a", 100.0)))
    }
}
