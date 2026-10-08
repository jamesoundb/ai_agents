package com.acme;

/** Calls through the interface: only dynamic dispatch reaches Square.area / Circle.area. */
public final class Areas {
    private Areas() {}

    static double total(Shape s) {
        return s.area();
    }
}
