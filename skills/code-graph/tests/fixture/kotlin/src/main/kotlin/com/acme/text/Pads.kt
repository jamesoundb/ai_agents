package com.acme.text

// `pad("x")` leaves `width` at its default: it calls the String overload, not the one-parameter List overload.
fun pad(
    text: String,
    width: Int = 8,
): String = text.padEnd(width)

fun pad(items: List<String>): String = items.joinToString()

fun label(): String = pad("x")

// A callable reference is a function value: it cannot be the Boolean, so `render("x", String::uppercase)`
// calls the transform overload.
fun render(text: String, upper: Boolean = false): String = text

fun render(text: String, transform: (String) -> String): String = transform(text)

fun shout(): String = render("x", String::uppercase)

// Positional arguments after a vararg stay on it, a named argument lands on its parameter and a trailing lambda on
// the last parameter: `index("by_name", unique = true, a, b) { … }` is the name-first overload.
fun index(unique: Boolean = false, vararg cols: String) {}

fun index(
    name: String? = null,
    unique: Boolean = false,
    vararg cols: String,
    types: List<String>? = null,
    kind: String? = null,
    filter: (() -> Boolean)? = null,
) {}

fun byName(a: String, b: String) = index("by_name", unique = true, a, b) { true }

// Named arguments out of order: only the overload with a `name` and a `flag` parameter can take them.
fun pick(name: String, flag: Boolean) {}

fun pick(count: Int, other: Boolean) {}

fun picked() = pick(flag = true, name = "n")

// `code` is typed only by its initializer, whose text has a `->` before a `<`: an argument type that stays
// unresolved is unknown, not that text (scoring it never terminated, and String-vs-text dropped the overload).
fun lint(code: String, strict: Boolean = false): Int = code.length

fun lint(lines: List<String>): Int = lines.size

class LintSpec {
    val code = """when (x) { 5 -> 1 }; if (x < 4) 2""".trimIndent()

    fun linted() = lint(code)
}

// An object named as an argument is a value of its own type: `table(Users) { … }` is the vararg overload, not the
// one whose first parameter is a Collection (both admit two arguments once defaults count).
open class Tbl

object Users : Tbl()

fun table(skip: Collection<String>, vararg tables: Tbl, setup: (() -> Unit)? = null, body: () -> Unit) {}

fun table(vararg tables: Tbl, setup: (() -> Unit)? = null, body: () -> Unit) {}

fun tabled() = table(Users) { }

// A qualified callable reference parses as a navigation (`Fmt.Case::up`): still a function value.
object Fmt {
    object Case {
        fun up(s: String): String = s.uppercase()
    }
}

fun shoutQualified(): String = render("x", Fmt.Case::up)
