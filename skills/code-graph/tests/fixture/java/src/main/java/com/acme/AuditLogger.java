package com.acme;

public class AuditLogger {
    public void log(PaymentRequest req) {}
    // Same name as List.add; must not be linked from PaymentOrchestrator.
    public void add(String s) {}
}
