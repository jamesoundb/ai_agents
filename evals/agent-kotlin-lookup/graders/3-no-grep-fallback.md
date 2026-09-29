---
type: tool_used
tool: Grep
min: 0
max: 0
---
Load-bearing here in a way it is not on the Python fixture, because on this one **grep gives
the wrong answer**. Verified on the fixture (2026-09-29), `grep -rn "place(" src/` returns five
lines across four files, including:

    src/main/kotlin/com/acme/legacy/LegacyOrderService.kt:11
        fun run(...): List<String> = orders.map { svc.place(it) }

`LegacyBatch.run` calls `LegacyOrderService.place` -- a different class in a different package
that happens to share the method name. Text matching cannot separate them. The graph can, and
does. So an agent that "verifies" its graph answer with grep does not merely waste tokens here,
it pulls a wrong caller into a correct answer.
