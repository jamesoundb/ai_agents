package com.acme;

public class Circle implements Shape {     // simple name, but Circle also nests a Shape
    public static class Shape {}
    public double area() { return 3.14; }
}
