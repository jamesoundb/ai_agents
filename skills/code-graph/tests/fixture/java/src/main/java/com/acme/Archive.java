package com.acme;

/** Two constructor overloads; the call chain from Reader.next() goes through the (String, int) one. */
public class Archive {
    private int size;

    public Archive(String name) {
        this.size = 0;
    }

    public Archive(String name, int size) {
        this.size = parseSize(size);
    }

    private int parseSize(int s) {
        return s;
    }
}

class Reader {
    Archive next() {
        return new Archive("entry", 512);
    }
}
