package com.acme;

import java.util.ArrayList;
import java.util.List;

public class PaymentOrchestrator extends BaseService {
    private PaymentGatewayClient gatewayClient;
    private AuditLogger auditLogger;
    private String label;

    public void processTransaction(PaymentRequest req, String name, String[] tags) {
        gatewayClient.validate(req);   // typed via field
        auditLogger.log(req);          // typed via field
        this.helper();                 // typed via inheritance (BaseService)
        helper();                      // implicit this: also typed via inheritance
        helper(3);                     // 1-arg overload, int literal
        this.helper("a", "b");        // 2-arg overload
        helper(name);                  // String param -> helper(String)
        helper(tags);                  // String[] -> helper(Object[])
        helper("lit");                 // string literal -> helper(String)
        helper(compute());             // argument is a call: typed from compute()'s return type -> helper(String)
        helper(this.label);            // argument is a field -> helper(String)
        helper(unknownFn());           // argument type unknown: same-arity overloads undecided -> ambiguous, not a typed guess
        List<String> l = new ArrayList<>();
        l.add("x");                    // external declared type -> unresolved, never AuditLogger.add
        getGateway().validate(req);    // method-return receiver -> typed
        var g = getGateway();
        g.validate(req);               // `var` local typed from the return type
    }

    public void varargs(Object... items) {
        helper(items);                 // varargs parameter is an array -> helper(Object[])
    }

    private String compute() { return "x"; }

    private PaymentGatewayClient getGateway() { return gatewayClient; }
}
