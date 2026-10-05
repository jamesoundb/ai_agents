package com.acme.msg;

/** Failure messages; callers bring the factories into scope with `import static`. */
public final class Messages {
    private Messages() {}

    public static String shouldContain(String actual) {
        return shouldContain(actual, 1);
    }

    public static String shouldContain(String actual, int count) {
        return actual + " should contain " + count;
    }
}
