package com.acme.shop

data class Order(val id: String, val total: Double) {
    fun withTax(rate: Double): Order = copy(total = total * (1 + rate))
}

enum class Channel(val code: Int) {
    WEB(1),
    STORE(2);

    fun isOnline(): Boolean = this == WEB
}
