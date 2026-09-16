package com.acme.shop

import com.acme.core.Base
import com.acme.core.Shape

class OrderServiceTest {
    fun testPlace() {
        val s = OrderService(OrderRepo())
        s.place(Order.create("1"))               // same package across src/main and src/test -> typed
    }

    fun testAnonymousObject() {
        val fixture = object : Base<Sq>(Sq(3)) {   // anonymous object: a class of its own
            val extra = Registry.lookup("9")
            override fun area(): Double = 9.0
        }
        fixture.describe()                       // inherited from Base -> typed
        fixture.extra?.sq?.area()                // member typed from its call result -> Sq.area
    }
}
