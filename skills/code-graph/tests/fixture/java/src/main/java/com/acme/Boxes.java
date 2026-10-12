package com.acme;

// `super.check(value)` with a boxed Long reaches check(Object): a `super` call is dropped only when its
// arguments certainly cannot be passed (assertj's isEqualTo(Float) -> super.isEqualTo(Object)).
class BaseBox {
    public String check(Object actual) {
        return "base";
    }
}

public class Boxes extends BaseBox {
    public String check(Long value) {
        return super.check(value);
    }

    public Boxes self() {
        return this;
    }

    public String tag() {
        return "tag";
    }

    public String label(String text) {
        return text;
    }

    public String label(Boxes box) {
        return "box";
    }

    // the argument's type is tag()'s (String), not self()'s (Boxes)
    public String chained() {
        return label(self().tag());
    }
}
