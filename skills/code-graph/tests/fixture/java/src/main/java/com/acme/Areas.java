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

    // type tests: `callers Circle` / `callers Square` list these branches (a pattern binding and a negated test)
    static String kind(Shape s) {
        if (s instanceof Circle c) return "circle " + c.area();
        return !(s instanceof Square) ? "other" : "square";
    }

    static String label(Shape s) {
        return switch (s) { case Square q -> "square"; default -> "shape"; };
    }
}
