package com.acme;

import static com.acme.msg.Messages.*;

/** Calls a static factory through an on-demand static import. */
public class WildChecks {
    public String check(String actual) {
        return shouldContain(actual);
    }
}
