package com.acme;

public class PaymentOrchestratorTest {
    public void testProcess() {
        new PaymentOrchestrator().processTransaction(new PaymentRequest());
    }
}
