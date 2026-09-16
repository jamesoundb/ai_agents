package com.acme;

// Fully-qualified interface name, and a nested class that shadows the interface's simple name.
public class Square implements com.acme.Shape {
    public static class Shape {}          // must NOT be what `implements` resolves to
    public double area() { return 1.0; }
}
