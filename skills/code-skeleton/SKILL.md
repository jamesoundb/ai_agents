---
name: code-skeleton
description: >
  Show a token-light tree-sitter skeleton of one or more source files (classes, functions,
  fields, signatures, decorators, calls, exact line ranges) before reading any raw code. Use
  proactively instead of cat/Read on any file longer than ~80 lines, and whenever asked "what is in
  this file/directory". Works for Python, JS/TS, Go, Java, Kotlin, Rust, Terraform and Kubernetes YAML.
allowed-tools: Bash(*/code-graph/scripts/run.sh *), Bash(*/code-skeleton/../code-graph/scripts/run.sh *), Read
---

# code-skeleton: index before you read

A skeleton costs roughly 5-15% of the tokens of a raw file of a few hundred lines or more (a
20,000-line module drops to 4%; a 40-line class still saves about two thirds) and gives exact
start/end lines for every item, so subsequent reads can target a range instead of the whole
file. (Maki, the tree-sitter agent discussed on Hacker News, measured ~165 net tokens saved per
turn from this pattern; the article's Java example drops from thousands of tokens per class to a
few hundred.)

```bash
../code-graph/scripts/run.sh skeleton $ARGUMENTS
```

Examples:
```bash
# one file
../code-graph/scripts/run.sh skeleton src/payments/orchestrator.py
# a directory (respects the default excludes: node_modules, .git, vendor, .terraform, ...)
../code-graph/scripts/run.sh skeleton src/payments --include '*.py'
# quieter output for very large files
../code-graph/scripts/run.sh skeleton big.ts --no-calls
```

Output format (per file):
```
src/payments/orchestrator.py  (python, 240 lines)
  imports: dataclasses, app.repo, .converters
  class PaymentOrchestrator  [L10-120]  @Service
    field gatewayClient: PaymentGatewayClient  [L12]
    def processTransaction(self, request: PaymentRequest) -> PaymentResult  [L30-80]  @Transactional
      calls: self.auditLogger.logAttempt, self.gatewayClient.validate, self.gatewayClient.charge
-- 1 file(s): raw ≈ 3,200 tokens, skeleton ≈ 310 tokens (90% saved)
```

## Then read only what you need

Use the line ranges: `Read` with `offset`/`limit`, or `sed -n '30,80p' FILE`. Do not read the
whole file unless the skeleton shows it is small or you must edit most of it.

## Notes

- A file with `parse errors` in its header still yields a partial skeleton; the model should
  treat the ranges near the error as approximate.
- Helm templates (files containing `{{`) are not valid YAML; the skeleton lists their `kind:`
  values only.
- For relationships across files (who calls this, what breaks if I change it) use the
  `code-graph` and `blast-radius` skills; the skeleton is single-file by design.
