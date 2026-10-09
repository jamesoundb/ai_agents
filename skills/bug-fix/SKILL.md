---
name: bug-fix
description: >
  Use when fixing a reported bug, failing test or wrong behaviour, before changing code: a checklist
  of the report's cases, a failing test per case, then the cause fixed and each case proven.
---

# bug-fix: every reported case, proven

A fix is done when every case the report states is proven by a test that failed before the fix
and passes after it -- not when the tests you happened to write pass. Incomplete fixes usually
cover the first variant of a bug and miss another one the report named, or test that variant with
data too weak to tell right from wrong. Expensive fixes usually harden or refactor code the cause
never touched. This procedure prevents both.

## Procedure

1. **Checklist from the report.** Before reading code, write a numbered list of every behaviour
   the report requires: each failing variant it names, each "must keep working" constraint, and
   the exact expected values it gives. This list is the definition of done; keep it in view.
2. **Locate the cause through the graph** (engine: `code-graph`'s `scripts/run.sh` called by its path
   from the working directory, e.g. `.claude/skills/code-graph/scripts/run.sh`, one command per call,
   never after `cd` into a skill folder; `build --root .` once if the repo has no `.ast-graph/`). `query source X` on the
   entry point the report names gives its code, callers and callees in one call; `query tests-for
   X` gives the existing tests to extend. When the report names a path that works (lazy vs eager
   loading, unbuffered vs buffered, one input type vs another), read that path too: the
   difference between the two is usually the cause.
3. **One failing test per checklist item**, in the existing test file for that code, built from the
   report's own example and asserting its exact expected values (not just "does not raise"). Use
   data that separates the variants (several parents, rows that must be excluded, values at the
   boundary). When the report states an item in words that allow several forms ("comparing a
   column with another", "a subclass", "an empty value"), test the form closest to what the code
   already treats as normal -- the one a wrong fix would mistake for normal: an equality where the
   code joins on equal keys, a subclass where it checks the base type, a value equal to the
   default -- or test each form. A test that passes however the fix classifies the item proves
   nothing. Run them: every bug item must fail for the reason the report describes; an item that
   already passes is a guard for a "must keep working" constraint.
4. **One hypothesis that explains every failing item.** If it explains only some, keep
   investigating before you edit. Look for the same defect at its sibling sites -- other branches of
   the same function, other overloads, duplicated logic, the callers `query callers` lists. A cause
   in two places needs two fixes.
5. **Fix the cause completely and nothing else**: no refactoring, renaming or hardening of code the
   cause does not touch.
6. **Prove it.** Run the checklist tests (all pass), then the tests of the package or module you
   changed, once. Re-read the checklist and tick each item against test output; an item without a
   passing test is not done. Do not run wider suites or repeat a check that already passed.
7. **Report** the cause (`file:line`), the fix, and one line per checklist item naming the test
   that proves it.
