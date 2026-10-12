package com.acme;

import java.io.OutputStream;
import java.io.Writer;

// `open(out, null)`: null fits the String parameter, so the OutputStream overload is the target (woodstox's
// createXMLEventWriter(out, null)).
public class Writers {
    public String open(OutputStream out) {
        return open(out, null);
    }

    public String open(OutputStream out, String enc) {
        return "stream";
    }

    public String open(Writer w, String enc) {
        return "writer";
    }
}
