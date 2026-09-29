---
name: code-skeleton
description: >
  Show a token-light tree-sitter skeleton of one or more source files (classes, functions,
  fields, signatures, decorators, calls, exact line ranges) before reading any raw code. Use
  proactively instead of cat/Read on any file longer than ~80 lines, and whenever asked "what is in
  this file/directory". Works for C/C++, Python, JS/TS, Go, Java, Kotlin, Rust, Terraform and
  Kubernetes YAML.
allowed-tools: Bash(*/code-graph/scripts/run.sh *), Bash(*/code-skeleton/../code-graph/scripts/run.sh *), Read
---

# code-skeleton: index before you read

A skeleton costs a fraction of the raw file and gives exact start/end lines for every item, so
subsequent reads can target a range instead of the whole file. Measured on this repository
(`~4 chars/token` estimate, which is what the tool reports):

| input | raw | skeleton | |
|---|---|---|---|
| a 5,400-line module (`astgraph.py`) | ≈69,986 | ≈8,239 | 89% less |
| a 650-line script (`tfreview.py`) | ≈9,754 | ≈937 | 91% less |
| a 590-line script (`analyze.py`) | ≈10,396 | ≈434 | 96% less |
| the whole `skills/` tree (106 files) | ≈146,756 | ≈20,338 | 87% less |

The saving grows with file size, so the skeleton is worth most on exactly the files you least want
to read whole. Below roughly 80 lines it stops paying for itself — see "Small files" below.

```bash
../code-graph/scripts/run.sh skeleton $ARGUMENTS
```

Examples:
```bash
# one file
../code-graph/scripts/run.sh skeleton src/payments/orchestrator.py
# a directory (respects the default excludes: node_modules, .git, vendor, build, dist, .terraform, ...)
../code-graph/scripts/run.sh skeleton src/payments --include '*.py'
# ... unless a default-excluded name is re-included
../code-graph/scripts/run.sh skeleton . --keep-dir build --include '*.yaml'
# quieter output for very large files
../code-graph/scripts/run.sh skeleton big.ts --no-calls
# bring back what the caps hid
../code-graph/scripts/run.sh skeleton big.ts --max-calls 30 --max-imports 0   # 0 = no cap
```

Output format (per file):
```
src/payments/orchestrator.py  (python, 240 lines)
  imports: dataclasses, app.repo, .converters
  class PaymentOrchestrator  [L10-120]  @Service
    field gatewayClient: PaymentGatewayClient  [L12]
    def processTransaction(self, request: PaymentRequest) -> PaymentResult  [L30-80]  @Transactional
      calls: self.auditLogger.logAttempt, self.gatewayClient.validate, self.gatewayClient.charge
-- elided: 12 calls (raise with --max-calls)
-- 1 file(s): ≈310 tokens here vs ≈3,200 to read them in full (90% less; ~4 chars/token estimate)
```

## Nothing is hidden without a way back

Each `calls:` line lists up to 8 entries and each `imports:` line up to 12; anything over shows
`(+N more)`. When a run elides something it prints one `-- elided:` line naming the flag that
lifts the cap — once per run, not per line. A truncated signature is recoverable from the
`[Lstart-Lend]` range printed beside it. If you see `(+N more)` and you need those entries,
re-run with `--max-calls`/`--max-imports` rather than falling back to reading the file.

## Then read only what you need

Use the line ranges: `Read` with `offset`/`limit`, or `sed -n '30,80p' FILE`. Do not read the
whole file unless the skeleton shows it is small or you must edit most of it.

## Small files

A skeleton has a fixed overhead (the header, the kind labels, the line ranges), so on a short
file it can cost more than the source. The tool refuses to do that: when a file is no larger
than its own skeleton it prints the header followed by the source itself, marked
`-- source is no larger than its skeleton, shown in full`, and a run that saves nothing overall
ends with `too small to summarize — read them directly` instead of a negative percentage. So
running it on a small file is never worse than reading it, just redundant — prefer `Read` when
you already know the file is short.

## Notes

- A file with `parse errors (first at LN)` in its header still yields a partial skeleton; the
  model should treat the ranges near that line as approximate.
- Helm templates (files containing `{{`) are not valid YAML; the skeleton lists their `kind:`
  values only.
- For relationships across files (who calls this, what breaks if I change it) use the
  `code-graph` and `blast-radius` skills; the skeleton is single-file by design.
