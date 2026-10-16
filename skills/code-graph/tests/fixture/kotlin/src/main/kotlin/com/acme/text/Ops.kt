package com.acme.text

import org.example.Schema

// Extensions on one generic type, told apart only by the receiver's type argument (the column argument is external,
// so untyped): Kotlin prefers `Op<String>.meets` over the generic one for an `Op<String>` receiver, and an
// `Op<Boolean>` receiver cannot take the String one at all.
class Op<T>(val value: T)

class Col<T>(val name: String)

fun Op<String>.meets(col: Col<String>, ignoreCase: Boolean = false): Boolean = ignoreCase

fun <T> Op<T>.meets(col: Col<T>): Boolean = true

sealed interface Cond

class NameCond(val op: Op<String>) : Cond

class FlagCond(val op: Op<Boolean>) : Cond

fun condition(c: Cond): Boolean =
    when (c) {
        is NameCond -> c.op.meets(Schema.NAME)
        is FlagCond -> c.op.meets(Schema.FLAG)
    }

fun flagged(o: Op<Boolean>): Boolean = o.meets(Schema.FLAG)

// An indexed receiver is an element of the list or array, type arguments kept (`ops[0]` of `List<Op<String>>`)
class Batch(val ops: List<Op<String>>, val flags: Array<Op<Boolean>>)

fun firstName(b: Batch): Boolean = b.ops[0].meets(Schema.NAME)

fun firstFlag(b: Batch): Boolean {
    val fs: Array<Op<Boolean>> = b.flags
    return fs[0].meets(Schema.FLAG)
}

// A repo extension on an external type is still the target (`stamps[0].millis()`, `d.millis()`)
fun java.time.LocalDateTime.millis(): Long = 0L

fun spread(stamps: List<java.time.LocalDateTime>): Long = stamps[1].millis() - stamps[0].millis()

fun one(d: java.time.LocalDateTime): Long = d.millis()

fun java.time.LocalDateTime.next(): java.time.LocalDateTime = this

fun later(d: java.time.LocalDateTime): Long {
    val n = d.next()
    return n.millis()
}

// `p.feed(this)`: `this` is the enclosing class, a Sink
interface Sink

interface Tap

class Pipe {
    fun feed(s: Sink): Int = 1

    fun feed(t: Tap): Int = 2
}

class Drain(val p: Pipe) : Sink {
    fun go(): Int = p.feed(this)
}

// Two-argument extensions told apart by the second argument (`Boolean` vs a converter) whose receivers also differ
// (`Op<String>` vs `<T> Op<T>`): the evidence names the receiver as well, since a check of the pick reads both.
fun Op<String>.near(col: Col<String>, ignoreCase: Boolean): Boolean = ignoreCase

fun <T> Op<T>.near(col: Col<String>, conv: (T) -> String): Boolean = true

fun nearName(c: NameCond): Boolean = c.op.near(Schema.NAME, true)
