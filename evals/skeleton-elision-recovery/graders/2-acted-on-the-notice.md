---
type: tool_used
tool: Bash
input_match: '--max-calls|callees'
min: 1
---
THE assertion this whole case exists for.

`charge()` makes 13 calls; the skeleton's cap shows 8 and prints `(+5 more)` plus
`-- elided: 5 calls (raise with --max-calls)`.

PASS means the model recovered the hidden calls with a second tool call -- either
re-running the skeleton with `--max-calls`, or asking the graph directly with
`query callees`. Both are legitimate; the notice names the first, the graph offers
the second.

FAIL means it ignored the notice: answered from the truncated list, or fell back to
reading the file whole -- which is the behavior the cap was supposed to prevent.

If this fails consistently across runs, the notice wording is wrong and the fix
belongs in `render_skeleton`/`elision_notice`, not in the model.
