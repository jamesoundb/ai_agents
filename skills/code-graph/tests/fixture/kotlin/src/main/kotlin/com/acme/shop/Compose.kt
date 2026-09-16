package com.acme.shop

annotation class Composable

@Composable
fun Row(modifier: String = "", content: @Composable RowScope.() -> Unit) {   // annotation on a function type: grammar workaround
    RowScope().content()
}

class RowScope

@Composable
fun Screen(title: String, trailing: @Composable () -> Unit = {}) {
    Row { }
    trailing()
}
