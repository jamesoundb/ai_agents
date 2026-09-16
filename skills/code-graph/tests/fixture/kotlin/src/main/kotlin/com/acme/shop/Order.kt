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

sealed interface Ev {
    data class Click(val id: Int) : Ev { fun describe(): String = "c" }
    data object Refresh : Ev
}

fun interface Handler {
    fun on(e: Ev)
}

class Widget(private val svc: OrderService) {              // plain constructor parameter: in scope for initialisers
    val lazyOrder: Order by lazy { Order("l", Sq(1)) }
    val h = Handler { e -> react(e) }                       // SAM constructor: calls the interface; lambda calls the outer member
    val cached = svc.place(Order("w", Sq(2)))               // property initialiser through a constructor parameter

    fun react(e: Ev): String {
        if (e is Ev.Click) e.describe()                     // smart cast in `if`
        return when (e) {
            is Ev.Click -> e.describe()                     // smart cast in `when`
            Ev.Refresh -> "r"
        }
    }

    fun names(list: List<Order>): List<String> = list.map(Order::describeTwice)   // callable reference: skip (Order has no describeTwice)
    fun sizes(): List<Int> = listOf(Sq(1)).map(Sq::describeTwice)                 // callable reference -> Sq.describeTwice
    fun again(): Order = lazyOrder.copy()                   // data-class-style copy on a repo type: typed as Order, no member edge
    fun kinds(): Kind = Kind.valueOf("SMALL")               // enum valueOf: no member edge, result typed as Kind
    fun code(): Int = Kind.valueOf("SMALL").code()          // -> Kind.code typed
}

class Runner(val svc: OrderService) {
    operator fun invoke(id: String): Order = Order(id, Sq(1))
}

class Consumer(private val runner: Runner) {
    fun go(): Order = runner("1")                           // `operator fun invoke` through a property
}
