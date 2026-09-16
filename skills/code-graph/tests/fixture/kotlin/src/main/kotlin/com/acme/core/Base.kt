package com.acme.core

interface Shape {
    fun area(): Double
}

abstract class Base<T : Shape>(protected val shape: T) {
    abstract fun area(): Double
    open fun describe(): String = "base"
}

fun fmt(x: Int): String = "i$x"
fun fmt(x: String): String = x

fun take(x: Any): Int = 0
fun take(x: Shape): Int = 1          // a Sq argument is a Shape: this overload wins over Any and over a generic
fun <T> take2(x: T): Int = 0
fun take2(x: Shape): Int = 1
