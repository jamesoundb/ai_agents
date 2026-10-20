package com.acme;

// tests-for fixture: the test reaches readMulti four calls down, past the default depth of 3.
public class Wkt {
    public String read(String text) {
        return read(text.toCharArray());
    }

    public String read(char[] chars) {
        return parse(chars, 0);
    }

    private String parse(char[] chars, int pos) {
        return readMulti(chars, pos);
    }

    private String readMulti(char[] chars, int pos) {
        return new String(chars, pos, chars.length - pos);
    }
}
