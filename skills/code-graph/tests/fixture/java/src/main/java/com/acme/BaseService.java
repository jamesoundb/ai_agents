package com.acme;

public class BaseService {
    protected void helper() {}
    protected void helper(int times) {}          // overload: arity decides which one a call attaches to
    protected void helper(String a, String b) {}
    protected void helper(String name) {}        // same arity as helper(int): argument type decides
    protected void helper(Object[] items) {}     // array vs scalar
}
