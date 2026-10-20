package com.acme;

public class WktTest {
    public void testReadMulti() {
        new Wkt().read("MULTIPOINT (1 2)");
    }
}
