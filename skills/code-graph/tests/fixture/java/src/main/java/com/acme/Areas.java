package com.acme;

/** Calls through the interface: only dynamic dispatch reaches Square.area / Circle.area. */
public final class Areas {
    private Areas() {}

    static double total(Shape s) {
        return s.area();
    }

    // a Square receiver binds the call to Square.area, which overrides Shape.area: callers of Shape.area list it
    static double squareArea(Square q) {
        return q.area();
    }
}
