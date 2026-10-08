package com.acme;

/** The abstract signature wraps its parameter list; the override does not. */
public abstract class Bootstrap {
    public abstract String open(String cfg, boolean main,
                                int version);

    public String open(String cfg,
                       boolean main) {
        return cfg;
    }

    static String start(Bootstrap b) {
        return b.open("x", true, 1);
    }
}
