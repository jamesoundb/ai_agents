package com.acme;

import static com.acme.msg.Messages.shouldContain;

/** Calls a static factory through a single-member static import. */
public class Checks {
    public String check(String actual) {
        return shouldContain(actual, 2);
    }
}
