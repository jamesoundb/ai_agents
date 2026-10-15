package com.acme;

// Overloads told apart only by the argument's type, a subtype of one parameter (JTS's
// Geometry.apply(CoordinateFilter) given a class implementing it): callers name the type that decided.
interface PointFilter {
    void filter(int x);
}

interface PartFilter {
    void filter(String part);
}

class CountFilter implements PointFilter {
    public void filter(int x) {
    }
}

public class Filters {
    public void apply(PointFilter f) {
    }

    public void apply(PartFilter f) {
    }

    public void count() {
        CountFilter counter = new CountFilter();
        apply(counter);
    }
}
