# evals/ — behavioral tests for the agents and skills

`skills/code-graph/tests/` proves the **engine** is correct: 229 assertions over a
nine-language fixture. Nothing there touches the layer users actually meet — whether
a model reading `SKILL.md` reaches for the right tool and then uses the result
properly. `tools/ci/smoke.sh` runs `--help` on each script, which proves the script
starts, not that the skill works.

This directory covers that gap.

## What each case asserts

| case | question it answers |
|---|---|
| `skeleton-before-reading` | Does a "what's in this file" request reach the skeleton instead of dumping the file? |
| `skeleton-elision-recovery` | Shown `-- elided: 5 calls (raise with --max-calls)`, does the model recover them — or give up? |
| `skeleton-small-file-guard` | Does a two-line file get *read*, rather than triggering the whole toolchain? |
| `blast-radius-impact` | Does an impact question traverse the graph, reach the transitive dependent, and cite `file:line`? |
| `code-graph-callers` | Does "who calls X" go through the graph rather than grep? |

`skeleton-elision-recovery` is the load-bearing one. The output-budget contract added
in 2026-09 only pays off if a model acts on the recovery notice. If that case fails
consistently, the fix belongs in `elision_notice()`, not in the model.

`skeleton-small-file-guard` is the counterweight: over-triggering costs tokens too, so
at least one case has to punish reaching for the graph when a `Read` would do.

## Running

One case format, two runners. A case written once is graded the same way on both.

```bash
# free, and catches typos before they cost tokens -- run this first, always
python3 evals/validate.py
```

### Antigravity (`agy`) — the one our developers have

```bash
python3 evals/run_agy.py                       # every case
python3 evals/run_agy.py --case 'skeleton-*'   # a subset
python3 evals/run_agy.py --runs 1 --no-judge   # cheap smoke pass
python3 evals/run_agy.py --json results.json
```

Each case runs in a throwaway workspace seeded from `case.yaml`'s `add_dirs`, with the
scaffold script run first. `--keep-temp` leaves workspaces on disk to inspect.

Graders name tools in Claude Code's vocabulary (`Bash`, `Read`, `Write`); `TOOL_ALIASES`
in `run_agy.py` maps them onto Antigravity's (`run_command`, `view_file`,
`write_to_file`). Native Antigravity names work too. Keep new graders in the Claude Code
vocabulary so they stay portable.

`llm` graders are scored by a second `agy` call constrained to answer PASS or FAIL.
`--no-judge` skips them and reports `skip` — never `ok`, so a skipped grader can't be
mistaken for a passing one.

### Claude Code

```bash
claude plugin eval . --json results.json
claude plugin eval . --case 'skeleton-elision-recovery' --runs 3
claude plugin eval . --trust-plugin --no-publish --threshold 0.8   # CI
```

Exit codes: `0` all cases at or above threshold, `1` below threshold or a load error,
`2` partial (cost ceiling or auth), `130`/`143` interrupted.

### A confound worth knowing about

`~/.gemini/GEMINI.md` holds personal global instructions that Antigravity loads into
every session. In practice that adds a preamble and sometimes extra file writes that
have nothing to do with the skills under test. `run_agy.py` prints a note when that file
exists. It is deliberately not suppressed by overriding `HOME`, because that breaks
`agy`'s auth — read around it instead.

### The tree-sitter prerequisite

Every case runs `scaffold/prepare.sh`, because eval runs get a **fresh HOME** and
`run.sh` caches its tree-sitter venv at `~/.cache/astgraph/venv`. Without the scaffold,
every single run would pip-install a 21 MB dependency tree (~30s, needs network).

To skip that entirely, point the launcher at a venv that already has the deps:

```bash
ASTGRAPH_VENV="$HOME/.cache/astgraph/venv" claude plugin eval .
```

## Adding a case

```
evals/<case-name>/
  prompt.md           # frontmatter + the exact text the agent receives
  graders/*.md        # one grader per file, scored independently, alphabetical
  case.yaml           # optional: scaffold_script, add_dirs
```

Grader types: `regex`, `tool_used`, `tool_order`, `file_exists`, `llm`, `baseline`.
Targets: `last_message`, `trace`, `files`, `mock_calls`, or `{source: file, path: ...}`.
`validate.py` knows the full field set for each and will reject the rest.

**Use deterministic graders wherever possible.** `tool_used` and `regex` cost nothing
and never flake. Reserve `llm` for genuine judgment calls, and write its `criteria`
with explicit PASS and FAIL conditions — vague criteria are the main source of judge
noise.

### One discipline that is not optional

**Verify every grader's assertion against real tool output before committing it.**

The first draft of `skeleton-elision-recovery` asserted that naming `_emit_metric`
proved the model had recovered the elided calls. It proved nothing: `_emit_metric` is
also a *method definition* in the same file, so it appears in the default skeleton
anyway. The case would have passed without the behavior it exists to test.

The fix was to move the hidden calls to imported functions in `src/telemetry.py`,
which appear nowhere else in `gateway.py`'s skeleton — then check it:

```bash
cd evals/fixtures/orders
../../../skills/code-graph/scripts/run.sh skeleton src/gateway.py | grep -c increment_counter   # 0
../../../skills/code-graph/scripts/run.sh skeleton src/gateway.py --max-calls 0 | grep -c increment_counter   # 1
```

A grader nobody checked is worse than no grader: it reports green and hides the gap.

## Status

**Executed and passing on Antigravity.** `run_agy.py` has been run against real
`agy` turns; see the worklog for traces. The headline result: given
`-- elided: 5 calls (raise with --max-calls)`, the agent re-ran with `--max-calls 0`
of its own accord and recovered all 13 calls — the first empirical evidence that the
output-budget contract does its job with a real model.

**Not executed on Claude Code.** `claude` is not installed on the machine where this
was written (`which claude` → nothing, no npx). The `claude plugin eval` file format
here came from documentation, not from a run, so expect to fix schema details the
first time someone tries it. The Antigravity path does not depend on any of that —
`run_agy.py` reads these files directly.

Also verified: the fixture provokes every behavior its graders assert on (each checked
by running the engine by hand), and `validate.py` was itself proven against six
injected faults.
