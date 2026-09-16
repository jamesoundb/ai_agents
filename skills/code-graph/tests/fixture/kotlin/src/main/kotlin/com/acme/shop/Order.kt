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

fun Sq.describeTwice(): Int = this@describeTwice.double().side + this.double().side   // `this` / `this@label` = the receiver Sq

enum class Kind { SMALL, LARGE; fun code(): Int = ordinal }

class Cart(val items: MutableList<Sq> = mutableListOf()) {
    private val all = mutableListOf<Sq>()
    fun total(): Double = all.sumOf { it.area() }              // `it` in a collection lambda on MutableList<Sq> -> Sq.area
    fun grow(): Cart = apply { all += Sq(1) }
    operator fun plus(o: Cart): Cart = Cart()
    inner class Audit {
        fun note(): Double = total()                            // inner class -> outer member
    }
    class Builder {
        fun kind(k: Kind) = apply { }                           // `= apply {}` returns the Builder
        fun build(): Cart = Cart()
    }
}

fun cart(block: Cart.() -> Unit): Cart = Cart().apply(block)   // DSL: lambda receiver is Cart
