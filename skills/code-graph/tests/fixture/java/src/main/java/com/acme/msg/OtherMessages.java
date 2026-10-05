package com.acme.msg;

/** Same method name in another class: never the target of an `import static ...Messages...` call. */
public final class OtherMessages {
    private OtherMessages() {}

    public static String shouldContain(String actual, int count) {
        return actual + " lacks " + count;
    }
}
