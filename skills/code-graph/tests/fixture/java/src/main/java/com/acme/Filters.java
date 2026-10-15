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

// A receiver that is an array element (`parts[i].apply(f)`, JTS's `geometries[i].apply(filter)`) is of the
// array's element type, so the argument picks the overload as on any typed receiver.
class FilterGroup {
    protected Filters[] parts;

    public void each(PointFilter f) {
        for (int i = 0; i < parts.length; i++) {
            parts[i].apply(f);
        }
    }

    public void first(PartFilter f) {
        Filters[] local = parts;
        local[0].apply(f);
    }
}

// `fs.apply(this)`: `this` is of the enclosing class, which implements PointFilter (JTS's
// AffineTransformation `g.apply(this)`), so the PointFilter overload is the target.
class SelfFilter implements PointFilter {
    public void filter(int x) {
    }

    public void run(Filters fs) {
        fs.apply(this);
    }
}
