package com.acme;

public final class LineBootstrap extends Bootstrap {
    // `super` call to the other overload: it must link to Bootstrap.open(String, boolean)
    public String open(String cfg, boolean main, int version) { return super.open(cfg, main); }
}
