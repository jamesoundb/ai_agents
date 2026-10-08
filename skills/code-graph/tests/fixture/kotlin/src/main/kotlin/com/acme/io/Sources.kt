package com.acme.io

// The base signature wraps its parameters one per line, as Kotlin style does.
abstract class Source {
    abstract fun read(
        path: String,
        limit: Int,
    ): String
}

class FileSource : Source() {
    override fun read(path: String, limit: Int): String = path
}
