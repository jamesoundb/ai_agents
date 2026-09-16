package com.acme.shop

import com.acme.core.Shape

data class Sq(val side: Int) : Shape {
    override fun area(): Double = side.toDouble()
}

class Order(val id: String, val sq: Sq) {
    companion object {
        fun create(id: String): Order = Order(id, Sq(1))
    }
}

fun Sq.double(): Sq = Sq(side * 2)          // extension function on a repo type
infix fun Sq.plus2(o: Sq): Sq = Sq(side + o.side)   // infix extension

object Registry {
    fun lookup(id: String): Order? = null
}
