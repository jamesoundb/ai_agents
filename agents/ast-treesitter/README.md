# AST Tree-sitter Agent

**Purpose.** Give humans and other agents a deterministic, token-light map of a codebase so that
architectural questions, impact analysis and navigation are answered from structure rather than
from raw file dumps. This is the organization's reference implementation of "context preparation
as a software engineering discipline": parsing, symbol indexing and call graphing are done by
tree-sitter before any model is involved; the model queries the map and drives.

Sources that shaped the design: Richard Whitney, *Stop Dumping Raw Code into LLMs: Why ASTs lead
to Scalable AI Agents* (Medium, 2026-09-06); Hacker News thread 47367129 (1M-context discussion,
tree-sitter/LSP/code-map approaches, Maki's `index` tool); the Copilot Chat OSS analysis gist by
intellectronica, section 3.1 "Context gathering mechanisms".

## What it is for

| Ask it | It returns |
|---|---|
| "Explain the architecture of the payments service" | Per-component cards (fields, methods, calls, annotations) plus cross-component edges and entry points |
| "What calls / instantiates / extends X?" | Transitive callers with `file:line`, edge type and confidence |
| "What breaks if I change this DTO / resource / ConfigMap?" | Blast Radius & Downstream Impact Matrix, affected files (direct/transitive), tests to run |
| "Where should this change go?" | Candidate symbols with locations and their importers |
| "How do our Terraform modules and Kubernetes objects connect?" | `module.x -> output.y`, `Service -> Deployment` selection, ConfigMap/Secret/PVC references |

## How it answers a question

Worth reading once, because the value is in the mechanism rather than the phrasing. A developer
asks *"who calls `Ledger.post`?"* in a Python package. The agent runs four commands:

```
run.sh build --root .                    # parse changed files, re-link the graph
run.sh query callers Ledger.post         # the answer
run.sh query symbol Ledger.post          # confirm the symbol it resolved
                                         # then open only the lines it is about to cite
```

and the second one returns:

```
callers of Ledger.post  (billing/core.py:12)  depth=2
  Ledger.post <- charge_account              [calls, typed]  (billing/api.py:10)
  Ledger.post <- settle                      [calls, typed]  (billing/jobs.py:9)
  Ledger.post <- test_post_records_an_entry  [calls, typed]  (tests/test_core.py:8)
```

No model reasoning produced that list. Tree-sitter parsed the files and the linker resolved the
receivers; the model asked the question and cited the result. Which is what makes it right where
the obvious alternative is wrong — in this package (`evals/fixtures/python-billing`, so you can
run it yourself):

- `billing/api.py` reaches the class through a re-export (`from .core import Ledger as Book`), so
  the defining name never appears in the calling file.
- `billing/jobs.py` holds a local typed only by a factory's return annotation
  (`def open_ledger(...) -> Ledger`), so the class name appears nowhere in that file either.
- `billing/legacy.py` has an unrelated `LegacyLedger.post`.

`grep -rn "Ledger"` is blind to the first two and `grep -rn "\.post("` reports the third as a
caller. The graph finds all three real callers, labels each `typed`, and excludes the decoy.
Measured over three runs: 8-9 tool calls, 33-51s, ~50k tokens, and the same trace shape every
time.

Not for: editing code (read-only by design), runtime/behavioral questions, or anything that needs
a real type checker: overloads are attributed only by argument count and the argument types the
linker can see (otherwise reported as `ambiguous`), generics resolve only through a declared
bound, and dynamic dispatch, reflection and DI-by-annotation are not followed. It states these
limits explicitly.

## Definition and install

- Developer guide: [`GETTING-STARTED.md`](GETTING-STARTED.md) (setup, how to ask, how to read an
  answer, the limits met first).
- Canonical definition: [`AGENT.md`](AGENT.md) (harness-neutral frontmatter + system prompt).
  `install.sh` at the repo root renders it into each harness's format (Claude Code subagent,
  Copilot custom agent, Antigravity custom agent, and an "agent-as-skill" wrapper for Codex and
  Gemini CLI)
  and installs the three skills side by side. See the root [README](../../README.md).
- Skills it uses (all in [`skills/`](../../skills)):

| skill | role | entry point |
|---|---|---|
| `code-graph` | build/refresh the graph; `find`, `symbol`, `callers`, `callees`, `path`, `file`, `overview`, `stats` | `skills/code-graph/scripts/run.sh` |
| `code-skeleton` | read-before-cat: signatures, members, calls, exact line ranges for a file or directory | `run.sh skeleton PATH...` |
| `blast-radius` | impact matrix for a file, symbol, Terraform address or Kubernetes object | `run.sh query trace-deps TARGET` |

Engine: `astgraph.py` (Python 3.10+, `tree-sitter`, `tree-sitter-language-pack`; developed and
tested on 3.12, and the pinned releases of both libraries require 3.10). `run.sh` installs
those into `~/.cache/astgraph/venv` on first use and stops with a clear message on an older
interpreter; override with `ASTGRAPH_PYTHON` (also used as the base for the venv) or
`ASTGRAPH_VENV`. Graph artifact: `.ast-graph/graph.db` plus a `graph.db.stamp` sidecar
(gitignore both). A build is skipped outright when the git working tree is unchanged; otherwise
files are re-parsed by content hash and the whole graph is re-linked.

### Storage: why the graph is a database

The graph was one JSON document that every query `json.load`ed in full. That is fine at a few
hundred files and fatal at twenty thousand: TensorFlow with C++ indexed produced a **1.3 GB**
`graph.json`, and a single `query stats` cost **12.8s and 5.3 GB of RSS**. Nearly two thirds of
those bytes were the per-file parse cache, which only `build` ever reads.

`graph.db` is SQLite: `nodes`, `edges`, `parsed` and `meta`, with eight indexes. A query fetches
what it needs through an index instead of materialising the graph, and lazy views (`NodeView`,
`AdjView`, `NameView`) keep `g.nodes[id]` and `g.out[src]` reading like dictionary access.
Measured on TensorFlow, same output both sides:

| query | JSON | SQLite |
|---|---|---|
| `symbol tensorflow::OpKernel` | 13.65s / 3.6 GB | **0.30s / 50 MB** |
| `callers OpKernel::Compute` | 3.15s / 688 MB | **0.29s / 47 MB** |
| `find MatMul` | 2.96s / 673 MB | **0.40s / 53 MB** |
| `stats` | 12.8s / 5.3 GB | 4.74s / 992 MB |

The queries a developer actually runs are ~100x leaner. `stats` and `overview` genuinely read the
whole graph, so they improved but stay slow, and **build got slower** (~11 min and 3.2 GB on
TensorFlow) because writing rows costs more than dumping a dict — the right trade when you build
once and query all day. `meta` stores `GRAPH_VERSION` and a hash of `astgraph.py`, so a graph
written by an older engine is rebuilt rather than misread; the previous JSON format is treated as
absent.
Regression tests: `skills/code-graph/tests/run_tests.sh` (fixture in `tests/fixture/`).

## Languages

C, C++ (and CUDA), Python, JavaScript, TypeScript/TSX, Go, Java, Kotlin, Rust, Terraform/HCL, Kubernetes YAML (including
Kustomization and Helm `values.yaml`; Helm templates are recognized but not parsed). Details and
limits: [`languages.md`](../../skills/code-graph/reference/languages.md).

## Guarantees and limits

- Deterministic: same inputs, same graph, independent of Python hash seeds and of whether files
  came from the cache or a fresh parse (verified by building TensorFlow and pandas under two
  `PYTHONHASHSEED` values and by comparing incremental against full builds). No model call is
  needed to build or query it.
- Every cross-file edge carries a confidence label (`exact`, `typed`, `same_file`, `package`,
  `import`, `unique`, `ambiguous`, `external`, `binding`). Consumers should treat `ambiguous` as
  leads; `binding` marks a Python call that reaches a C++ implementation through pybind11 or
  `REGISTER_OP`, so the chain is real but crosses a language boundary.
- Receiver- and import-aware: a call is linked to a repo symbol only when the receiver is a
  resolved repo type (matched by node identity, ancestors included), an import binding (module
  names bind to module files, re-exports are followed), or `self`/`this`. Calls on external or
  builtin receivers, unqualified builtins, and bare names that are neither imported nor in scope
  are left unresolved; values of unknown type get at most three same-language `ambiguous` leads.
- Syntactic only: overloads are chosen by arity and by the argument types the linker can see
  (declared parameters, typed locals, literals, fields, call results); an undecided call is
  recorded as `ambiguous` across the overload set rather than guessed. Generic parameters resolve
  through their first declared bound only. No dynamic dispatch, reflection or DI-by-annotation;
  interface calls resolve to the interface method (follow `implements` edges for implementations).

## Adoption checklist for a repository

1. From a clone of this repo: `./install.sh --harness antigravity --target /path/to/your/repo` (or your harness)
   (add `--copy` if symlinks are not an option, e.g. Windows without developer mode).
   To roll out this agent alone, without the infrastructure ones, name it:
   `./install.sh --harness claude --target /path/to/repo --agents ast-treesitter`. Its three
   skills (`code-graph`, `code-skeleton`, `blast-radius`) come with it because the agent declares
   them, so the relative path `code-skeleton` uses to reach the engine
   (`../code-graph/scripts/run.sh`) cannot end up dangling.
2. Add `.ast-graph/` to the repo's `.gitignore`.
   Claude Code: the subagent runs with `permissionMode: default` and skills preloaded through a
   subagent's `skills:` list do not carry their `allowed-tools` pre-approval, so each engine call
   prompts unless `.claude/settings.json` allows it, for example
   `"permissions": {"allow": ["Bash(*/code-graph/scripts/run.sh *)"]}`.
3. Run `<skills dir>/code-graph/scripts/run.sh build --root .` once and check `query stats`
   shows the expected languages and file counts. Add `--exclude` globs or extend `EXT_LANG` if not.
4. Optional CI: rebuild the graph on the default branch and publish `overview --json` as an
   architecture snapshot; run `trace-deps` for each changed file on pull requests.
5. Repositories with more than one language: a name that exists in both halves is **not** guessed.
   `query callers OrderService.place` across a Kotlin service and a Python one answers

   ```
   'OrderService.place' is ambiguous (2 matches). Re-run with one of these ids or `file:name`:
     api/src/main/kotlin/com/acme/OrderService.kt::OrderService.place@6
     svc/orders.py::OrderService.place@12
   ```

   `query callers OrderService.kt:OrderService.place` picks one side. Tell developers this is the
   expected answer rather than a failure: the alternative is a tool that silently reports the
   callers of the wrong `place`.

## Measured behaviour

The tables below were measured on the **JSON engine**, before the SQLite migration of
2026-09-28. Node and edge counts, resolution quality and build times still hold; the query
latency and memory figures have been superseded by the table above, and rows that describe JSON
mechanics are marked.

Large repo (terraform-provider-google @ e4cfd727a, 2,933 Go files, 1.19M lines, 2026-09-14):

| run | result |
|---|---|
| cold build | 13s, 346 MB peak RSS, 50,341 nodes, 224,417 edges |
| unchanged git tree | fast path, 0.13s |
| one-file edit | 1 file re-parsed, 6s (JSON era: load/dump plus a ~4s linear re-link) |
| `query overview` / `query stats` | ~1.7s each (JSON era: loaded the whole 188 MB graph; SQLite queries do not) |
| `trace-deps ReplaceVars --depth 2` | 1,029 files; 5,697 rows degrade to the summary view automatically |
| `symbol ReplaceVars` | 47 lines (capped, with a per-file summary of the rest) |

Before the receiver-aware linker (same repo, same day) a test mock ranked as the #1 hub with
32,683 name-matched incoming edges and 12 of 13 "tests to run" were not tests.

Python-heavy repos (2026-09-14, after the import-aware linker; "before" = same day, receiver-aware
linker only):

| | pandas (717k LOC py) | TensorFlow (1.24M LOC py + go/java; C++ not indexed) |
|---|---|---|
| cold build | 12s, 256 MB RSS | 20s, 510 MB RSS |
| one-line edit rebuild | 4.9s | 11.8s |
| graph.json (JSON era; now `graph.db`) | 162 MB -> 107 MB | 375 MB -> 257 MB |
| edges (before -> after) | 407,580 -> 177,601 | 810,251 -> 410,035 |
| ambiguous edges | 281,255 -> 25,408 | 529,471 -> 79,480 |
| typed edges | 6,407 -> 14,678 | 42,134 -> 52,295 |
| `overview --no-tests` top hubs | DataFrame, Index, Series, NDFrame, MultiIndex | Graph, Operation, cast, convert_to_tensor, Context |
| `trace-deps convert_to_tensor` direct files | | 1 -> 345 (text cross-check: 343) |
| query latency | 1.5s | 3.5s -> 2.7s |

Known blind spots on those repos: Cython (`.pyx`), C/C++, Bazel-generated `gen_*_ops.py`
modules that are not in a source checkout, and runtime registries (tensor conversion functions,
dispatch decorators).

Other languages (2026-09-15):

| repo | files | cold build | typed / import / ambiguous edges | notes |
|---|---|---|---|---|
| vite (TS/JS pnpm monorepo) | 1,596 | 2.7s, 73 MB | 407 / 3,768 / 2,690 | workspace `vite` imports and `index.ts` barrel re-exports resolve (`ResolvedConfig`: 43 direct files, text check 44); 244 asset imports counted, not linked |
| apache/commons-lang (Java) | 635 | 6s, 113 MB | 29,032 / 493 / 25,694 | overloads attributed by arity and argument type (varargs, literals, fields, call results); calls into an overload set with an argument the linker cannot type are `ambiguous` across the set instead of a `typed` guess, hence the large ambiguous count; `implements Builder<T>` resolves for all 9 implementers; method-return receivers typed |
| JetBrains/Exposed (Kotlin, Gradle multi-module) | 888 | 6.9s, 256 MB | 13,709 / 8,528 / 24,061 | package resolution shared with Java; companions, extension and infix functions, anonymous objects, properties typed from DSL builder calls; the infix `eq` operator has 678 typed callers in 132 files; `currentDialect.functionProvider.charLength()` typed through an imported top-level `val`; `FunctionProvider.charLength` card lists its three dialect overrides |
| terraform-google-modules/terraform-google-kubernetes-engine (HCL, 7 generated module copies) | 658 (hcl 541, yaml 77, go 38) | 1.0s, 59 MB | 8 / 0 / 57 (references: 9,322 exact) | every `var.`/`local.`/`module.` reference resolved at its own line; 76 `moved` blocks linked to their targets; `dynamic` iterators no longer counted as unresolved (2,588 -> 1,191); registry-sourced examples reach the local sub-module as leads; `overview` collapses the seven same-named copies |
| ripgrep (Rust workspace) | 115 | 1.3s, 54 MB | 2,889 / 1,676 / 3,284 | `crate::`, `super::`, `self::`, grouped and cross-crate `use`, `pub use` re-exports resolve; generic-bounded fields reach trait methods; `?`-unwrapped locals typed |
| pallets/flask (Python, 2026-09-16) | 91 | 0.4s, 47 MB | 323 / 672 / 304 | aliased base class (`Blueprint as SansioBlueprint`) resolved so the blueprint hierarchy and `Blueprint.add_url_rule` dependents exist; pytest-fixture parameters typed from the fixture's return annotation (`app.register_blueprint` callers in tests); `**options.pop()` no longer leads into repo `pop` methods; `path Flask Scaffold` is the extends chain |
| android/nowinandroid (Kotlin, Compose, Hilt, Flow; 2026-09-16) | 357 | 0.7s, 59 MB | 689 / 840 / 261 | every `@Composable` component indexed (the grammar rejects `@Composable () -> Unit` parameters; the annotation is blanked before parsing: 11 -> 0 parse-error files); `getFollowableTopics()` resolves to `operator fun invoke`; `TopicEntity::asExternalModel` callable references are calls; `core/testing/src/main` is production, `src/test` and `src/androidTest` are tests; GitHub Actions `${{ }}` no longer counted as Helm templates |
| encode/httpx (typed async Python; 2026-09-16) | 66 | 0.4s, 47 MB | 746 / 1,339 / 105 | ambiguous down from 366: string-literal and dict-literal receivers, builtin-typed values and `self._pool` (assigned in an `if` branch of `__init__` from an external constructor) no longer lead anywhere; `super().__init__()` resolves to `BaseClient.__init__`; `with httpx.Client() as client:` types `client` (11 test files reach `Client.get`); `callers Response --no-members` isolates the 295 instantiation rows |
| square/okhttp (Kotlin + Java, 2026-09-16) | 692 | 5.3s, 125 MB | 23,014 / 2,700 / 6,582 | typed edges up from 17,379: `chain.proceed()` on a parameter typed `Interceptor.Chain` (nested type of an import), `implements Interceptor.Chain`, `val realChain = chain as RealInterceptorChain`, inner-class calls to outer members, multi-line `Request\n .Builder()` chains; `RealInterceptorChain.proceed` prints the dispatch note with its 86 base-method callers |

After the same round, the Python repos: pandas 16,611 typed / 28,307 ambiguous / 0 unique (build
17s), TensorFlow 78,698 typed / 65,120 ambiguous / 0 unique (build 34s). Return-type inference
adds roughly a third to build time on Python-heavy repos.

Fixture (`skills/code-graph/tests/fixture`, 76 indexed source files across eleven languages plus
build metadata such as `go.mod`, `Cargo.toml`, `package.json` and `tsconfig.json`; the suite has
234 check sites, 285 executed assertions; counts re-verified 2026-09-29):

- Full build well under a second; incremental rebuild re-parses only changed files (1 of 69 after
  a one-line edit) and yields the same node and edge sets as a full build.
- Java example from the article reproduced: `PaymentOrchestrator` card shows injected fields and
  `processTransaction` calls resolved as `typed`; changing `PaymentRequest` lists the interface
  and class API contracts, the gateway client, the audit logger and the test.
- Skeleton of a 2,000-line Python file: ~26.9k raw tokens vs ~3.9k skeleton tokens (86% saved).

### Behaviour of the agent, not the engine

Everything above measures the graph. These measure whether the agent *uses* it well, which is a
separate claim and needs a model in the loop. Cases live in [`evals/`](../../evals); each was run
three times on Antigravity (2026-09-29), in a workspace isolated from this checkout.

| case | asserts | result |
|---|---|---|
| `agent-direct-lookup` | a one-line question takes the fast path (build + one query), no `overview`, no grep | 12/12 |
| `agent-python-resolution` | callers reachable only through a re-export alias and a factory's return annotation, excluding a same-named decoy | 21/21 |
| `agent-kotlin-lookup` | the same in Kotlin, past a same-named method on an unrelated class | 18/18 |

7-9 tool calls, 33-51s and 44k-69k tokens per question, with the same trace shape every run:
read the skill, `build`, `query callers`, `query symbol`, then open only the lines it is about to
cite. The two resolution cases matter more than the pass rate suggests — their fixtures are built
so that `grep` returns both false negatives and a false positive, so they assert that the answer
is *correct*, not merely that it was cheap.

## Related agents

The `terraform`, `helm` and `kubernetes` agents declare `code-graph`, `blast-radius` and
`code-skeleton` in their `skills:` list and `build-pipeline` declares `code-graph` and
`code-skeleton`, so they answer structural questions through the same engine instead of
re-implementing parsing; the HCL/YAML extractors live here so that all platform agents share one
map. For a full architecture or impact question, delegate to this agent.
