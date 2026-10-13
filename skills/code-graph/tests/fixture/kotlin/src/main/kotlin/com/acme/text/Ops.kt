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
