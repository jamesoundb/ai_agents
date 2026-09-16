package com.acme.shop

import com.acme.core.Base
import com.acme.core.fmt
import com.acme.core.*

class OrderRepo {
    fun save(o: Order): Order = o
    fun all(): List<Order> = emptyList()
}

fun makeRepo(): OrderRepo = OrderRepo()

@Service
class OrderService(val repo: OrderRepo) : Base<Sq>(Sq(2)) {
    val cached = makeRepo()                       // class property typed from the callee's return type
    override fun area(): Double = shape.area()      // generic-bounded field -> Shape.area (typed)

    fun place(o: Order, vararg tags: String): Order {
        val saved = repo.save(o)                  // typed via constructor property
        val made = Order.create("x")              // companion member -> typed
        made.sq.double()                          // extension function on Sq -> typed
        saved.sq.area()                           // field chain -> Sq.area typed
        fmt(1)                                    // overload by literal type -> fmt(Int)
        fmt("a")                                  // -> fmt(String)
        this.describe()                           // inherited across packages -> typed
        describe()                                // implicit this
        val found = Registry.lookup("1")          // object member -> typed; local typed Order?
        found?.sq?.area()                         // safe calls -> typed Sq.area
        val n = repo.all()
        n.first()                                 // List (external) -> unresolved
        println(fmt(tags.size))                   // builtin println: no edge
        OrderRepo().save(o).sq.double()           // inner calls of a chain are recorded: OrderRepo, save, double
        made.sq plus2 saved.sq                    // infix call -> Sq.plus2 typed
        made.let { it.sq }                        // stdlib scope function: no lead edge
        n.forEach { }                             // stdlib collection function: no lead edge
        cached.save(o)                            // -> OrderRepo.save typed via the property's call result
        val reg = Registry
        reg.lookup("3")                           // local bound to an object reference -> typed
        take(saved.sq)                            // subtype-aware overload: take(Shape), not take(Any)
        take2(saved.sq)                           // take2(Shape) beats the generic take2(T)
        currentDialect.functionProvider.charLength()   // imported top-level val -> property chain -> typed
        return saved
    }
}
