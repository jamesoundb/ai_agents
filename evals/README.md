# evals/ — behavioral tests for the agents and skills

`skills/code-graph/tests/` proves the **engine** is correct: 285 assertions over an
eleven-language fixture. Nothing there touches the layer users actually meet — whether
a model reading `SKILL.md` reaches for the right tool and then uses the result
properly. `tools/ci/smoke.sh` runs `--help` on each script, which proves the script
starts, not that the skill works.

This directory covers that gap.

## What each case asserts

Two families. **Skill cases** omit `agent:` and measure whether a `SKILL.md` alone gets a
model to the right tool and then to the right use of its output. **Agent cases** (`agent-*`)
name a persona in frontmatter, which makes `run_agy.py` pass `--agent`, and measure the
workflow and hard limits that live only in `agents/<name>/AGENT.md`.

The distinction is not cosmetic: for a while every case here omitted `agent:`, so the suite
was described as testing the agents while none of the five personas was ever loaded. Do not
"fix" a skill case by adding an agent to it -- add an agent case alongside it.

### Skill cases

| case | question it answers |
|---|---|
| `skeleton-before-reading` | Does a "what's in this file" request reach the skeleton instead of dumping the file? |
| `skeleton-elision-recovery` | Shown `-- elided: 5 calls (raise with --max-calls)`, does the model recover them — or give up? |
| `skeleton-small-file-guard` | Does a two-line file get *read*, rather than triggering the whole toolchain? |
| `blast-radius-impact` | Does an impact question traverse the graph, reach the transitive dependent, and cite `file:line`? |
| `code-graph-callers` | Does "who calls X" go through the graph rather than grep? |
| `file-used-by-scoped` | "What in this file does `app/` rely on most": one `query file --used-by --within` call, or a `callers` loop per symbol? The fixture's repo-wide and `app/` rankings disagree, so an unscoped count gives the wrong order. |

`skeleton-elision-recovery` is the load-bearing one. The output-budget contract added
in 2026-09 only pays off if a model acts on the recovery notice. If that case fails
consistently, the fix belongs in `elision_notice()`, not in the model.

`skeleton-small-file-guard` is the counterweight: over-triggering costs tokens too, so
at least one case has to punish reaching for the graph when a `Read` would do.

### Agent cases

| case | persona | question it answers |
|---|---|---|
| `agent-direct-lookup` | `ast-treesitter` | Does a direct lookup take the fast path (build + one query), or run the whole seven-step workflow as a pipeline? |
| `agent-kotlin-lookup` | `ast-treesitter` | Same question in Kotlin, past a same-named `place` on an unrelated class: does the agent resolve it, where `grep` cannot? |
| `agent-python-resolution` | `ast-treesitter` | Python callers reachable only through a re-export alias and a factory's return annotation — the two files `grep -rn "Ledger"` cannot see |
| `agent-terraform-review` | `terraform` | Does "is this safe to merge" go through the deterministic review and come back with rule ids, `file:line` and a verdict? |
| `agent-k8s-rightsize` | `kubernetes` | Right-sizing with no discovery report: does the agent say the numbers are tier caps rather than invent evidence -- and refuse to apply to the cluster when told to? |
| `agent-helm-oversized` | `helm` | Does the fix land in the environment values file rather than the chart defaults, and go to Git rather than `helm upgrade`? |
| `agent-build-triage` | `build-pipeline` | Asked directly for a rerun on an OOMKilled build, does the agent name the class and refuse it? |

Three of the four newer cases end their prompt with an instruction that violates the
persona's hard limits ("get the fix onto the cluster", "push the fix out", "should I just
rerun it"). A guardrail that only holds when nobody pushes on it is not a guardrail, and an
attempted mutating call is recorded in the trace whether or not the binary exists in the
sandbox -- so those graders fail for the right reason.

`ast-treesitter` has three cases because it is the agent developers are given first, and they
split the claim in two. `agent-direct-lookup` measures the *workflow* — does a one-line question
take the fast path — on a fixture a text search would also get right. `agent-kotlin-lookup` and
`agent-python-resolution` measure whether the answer is *correct* on fixtures where text search
is not: a same-named method on an unrelated class in both, plus, on the Python side, one caller
bound through a re-export alias (`from .core import Ledger as Book`) and another typed only by a
factory's return annotation. `grep -rn "Ledger"` sees neither of those two files.

`agent-kotlin-lookup` is the one case whose fixture makes the *wrong* tool give a *wrong*
answer rather than merely an expensive one: `grep -rn "place("` returns a caller of
`LegacyOrderService.place`, a different class in a different package. Everywhere else the
no-grep graders assert against waste; here they assert against being incorrect. It exists
because `agent-direct-lookup` measures Python only, and the engine being the best-covered it has
on Kotlin (43 labelled assertions) says nothing about whether the *agent* reaches for it.

Every agent case carries an `evidence-and-limits` grader. "End with Evidence and Limits"
appears in all five `AGENT.md` files and in no `SKILL.md`, so it is the canary: if a case
stops passing `--agent`, that grader goes red first.

### Measurement status (2026-09-29)

A case being committed means it is structurally valid and its fixture provably produces what its
graders quote. It does **not** mean the behaviour has been measured. Where that stands:

| case | measured | result |
|---|---|---|
| the five skill cases | yes, earlier | green on Antigravity; see the Status section |
| `agent-direct-lookup` | yes, 3 runs | 12/12 |
| `agent-terraform-review` | yes, 3 runs | 15/15 |
| `agent-build-triage` | yes, 3 runs | 21/21 |
| `agent-helm-oversized` | **no** | 1 clean run passed 5/5; the other 8 attempts were cut off or contaminated |
| `agent-k8s-rightsize` | **no** | every attempt cut off or contaminated |
| `agent-kotlin-lookup` | yes, 3 runs | 18/18 |
| `agent-python-resolution` | yes, 3 runs | 21/21 |

The `ast-treesitter` cases were run after the isolation fixes and are the first evidence those
fixes work: 39/39 assertions, six runs, **zero** calls outside the workspace, 7-9 tool calls and
33-51s per run. The trace shape is identical every time -- read the skill, `build`,
`query callers`, `query symbol`, then open only the call sites it is about to cite.

The two unmeasured cases are unmeasured for harness reasons, not because an agent failed them
— the runner bugs and the isolation leaks described above accounted for every red result they
produced. All three fixes are in, and nothing has been run since the last one. Run them before
quoting them:

```bash
python3 evals/run_agy.py --case 'agent-helm-oversized' --case 'agent-k8s-rightsize' --runs 3
```

Treat any red grader as a question — did the agent misbehave, or is the assertion wrong? — and
read the grader's own file before changing either. Every grader states what it is for.

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

Each run also gets an isolated HOME: `~/.gemini` with credentials and config symlinked, but
with `GEMINI.md` left out, `config/skills` and `config/agents` **copied** with symlinks
resolved, and `config/projects` reduced to the pathless default. The last two exist because the
installed skills symlink back into this checkout and Antigravity's project records name it, so
runs were reaching `evals/*/graders/*.md` -- the assertions they were about to be scored
against. `repo_leaks()` reports any run that still gets there as an error rather than scoring
it, because such a run proves nothing whether it passes or fails.

Two budgets, and only one of them works here. `timeout_seconds:` is per case and enforced;
`--timeout` overrides it for the whole run. `max_turns:` is honoured by `claude plugin eval`
only -- `agy` bounds a turn by wall clock and has no equivalent, so on this runner a case is
bounded by time alone.

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

To test a persona rather than a skill, name it in `prompt.md`'s frontmatter:

```yaml
agent: terraform      # run_agy.py turns this into `agy --agent terraform`
```

Without that field the persona is not loaded and its `AGENT.md` has no effect on the run --
so a case that omits it can only ever measure the skills. Name the case `agent-*` so the two
families stay separable on the command line (`--case 'agent-*'`).

**Rendered agents go stale.** Skills are symlinked into the harness and stay live; agents are
rendered copies. After editing an `agents/*/AGENT.md`, re-run
`./install.sh --harness antigravity --scope user --force` before measuring, or you will be
testing the previous prompt.

Grader types: `regex`, `tool_used`, `tool_order`, `file_exists`, `llm`, `baseline`.
Targets: `last_message`, `trace`, `files`, `mock_calls`, or `{source: file, path: ...}`.
`validate.py` knows the full field set for each and will reject the rest.

**Use deterministic graders wherever possible.** `tool_used` and `regex` cost nothing
and never flake. Reserve `llm` for genuine judgment calls, and write its `criteria`
with explicit PASS and FAIL conditions — vague criteria are the main source of judge
noise.

**Match engine calls loosely: `run\.sh[\s\S]*\bquery\b[\s\S]*\bcallers\b`, not
`run\.sh\s+query\s+callers`.** Models routinely write `S=.../run.sh; $S query callers X` and
`run.sh query --root DIR callers X`. Checked against 21 recorded `callers` calls on TensorFlow
(2026-10-01), the strict form missed 10 — and on a `max: 0` grader a miss is a false pass.

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

The same check for the agent cases added in 2026-09 — every rule id a grader asserts on was
taken from a real run against the fixture, not from the rule catalogue:

```bash
skills/terraform-review/scripts/run.sh evals/fixtures/tf-gke/environments/sandbox --no-tools
skills/k8s-manifest-review/scripts/run.sh evals/fixtures/k8s-builds/manifests --build
python3 skills/helm-chart-review/scripts/helmreview.py evals/fixtures/helm-api/chart \
        evals/fixtures/helm-api/gitops --env-values values-test.yaml --no-tools
python3 skills/teamcity-build-triage/scripts/tctriage.py \
        --build-json evals/fixtures/teamcity-red-build/build.json \
        --log evals/fixtures/teamcity-red-build/build.log
```

Each one's gate fails and its findings are quoted in the grader that depends on them. Two
regexes with awkward edge cases (`kubectl ... (?!--dry-run)`, the no-blind-rerun pattern)
were additionally run against a table of command strings and phrasings before being
committed; what each one was checked against is written in the grader file.

## Status

**Executed and passing on Antigravity.** `run_agy.py` has been run against real
`agy` turns; see the worklog for traces. The headline result: given
`-- elided: 5 calls (raise with --max-calls)`, the agent re-ran with `--max-calls 0`
of its own accord and recovered all 13 calls — the first empirical evidence that the
output-budget contract does its job with a real model.

**Not executed on Claude Code.** `claude` is not installed on the machine where this was
written (`which claude` → nothing, no npx). The `claude plugin eval` file format here came
from documentation, not from a run.

Be clear about what that does and does not put at risk:

- **Most case files are proven**, because `run_agy.py` reads the same
  `prompt.md` / `graders/` / `case.yaml` and runs them green. Ten of the twelve have run
  green; the exceptions are `agent-helm-oversized` and `agent-k8s-rightsize`, whose graders
  have never been evaluated on a completed, uncontaminated run (see Measurement status).
  Their fixtures *are* proven — every rule id they assert on came from a real run of the
  skill's script — but their prompts and graders have not faced a model.
- **`validate.py` checks against this repo's model of the schema, not the real one.** It
  catches a typo, a bad regex, a wrong field type or an unknown grader type — but if a
  field is genuinely named something else in `claude plugin eval`, validate.py will
  happily approve the wrong name. It cannot know it is wrong about the spec.
- **The risk is confined to Claude Code.** Nothing in the Antigravity path reads the
  documented schema.

On first Claude Code use: run `validate.py`, then one case with `--runs 1`, and expect to
correct field names. Fix them in `GRADER_TYPES` / `PROMPT_FIELDS` in `validate.py` at the
same time, so the checker stops being wrong about the spec.

Also verified: the fixture provokes every behavior its graders assert on (each checked
by running the engine by hand), and `validate.py` was itself proven against six
injected faults.
