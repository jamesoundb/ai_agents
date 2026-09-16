package com.acme;

public class PaymentGatewayClient {
    public boolean validate(PaymentRequest req) { return req.amount > 0; }
}
