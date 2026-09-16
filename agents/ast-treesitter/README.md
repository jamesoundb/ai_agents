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

Not for: editing code (read-only by design), runtime/behavioral questions, or anything that needs
a real type checker: overloads are attributed only by argument count and the argument types the
linker can see (otherwise reported as `ambiguous`), generics resolve only through a declared
bound, and dynamic dispatch, reflection and DI-by-annotation are not followed. It states these
limits explicitly.

## Definition and install

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
`ASTGRAPH_VENV`. Graph artifact: `.ast-graph/graph.json` plus a `graph.json.stamp` sidecar
(gitignore both). A build is skipped outright when the git working tree is unchanged; otherwise
files are re-parsed by content hash and the whole graph is re-linked.
Regression tests: `skills/code-graph/tests/run_tests.sh` (fixture in `tests/fixture/`).

## Languages

Python, JavaScript, TypeScript/TSX, Go, Java, Kotlin, Rust, Terraform/HCL, Kubernetes YAML (including
Kustomization and Helm `values.yaml`; Helm templates are recognized but not parsed). Details and
limits: [`languages.md`](../../skills/code-graph/reference/languages.md).

## Guarantees and limits

- Deterministic: same inputs, same graph, independent of Python hash seeds and of whether files
  came from the cache or a fresh parse (verified by building TensorFlow and pandas under two
  `PYTHONHASHSEED` values and by comparing incremental against full builds). No model call is
  needed to build or query it.
- Every cross-file edge carries a confidence label (`exact`, `typed`, `same_file`, `package`,
  `import`, `unique`, `ambiguous`, `external`). Consumers should treat `ambiguous` as leads.
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

1. From a clone of this repo: `./install.sh --harness all --target /path/to/your/repo`
   (add `--copy` if symlinks are not an option, e.g. Windows without developer mode).
2. Add `.ast-graph/` to the repo's `.gitignore`.
   Claude Code: the subagent runs with `permissionMode: default` and skills preloaded through a
   subagent's `skills:` list do not carry their `allowed-tools` pre-approval, so each engine call
   prompts unless `.claude/settings.json` allows it, for example
   `"permissions": {"allow": ["Bash(*/code-graph/scripts/run.sh *)"]}`.
3. Run `<skills dir>/code-graph/scripts/run.sh build --root .` once and check `query stats`
   shows the expected languages and file counts. Add `--exclude` globs or extend `EXT_LANG` if not.
4. Optional CI: rebuild the graph on the default branch and publish `overview --json` as an
   architecture snapshot; run `trace-deps` for each changed file on pull requests.

## Measured behaviour

Large repo (terraform-provider-google @ e4cfd727a, 2,933 Go files, 1.19M lines, 2026-09-14):

| run | result |
|---|---|
| cold build | 13s, 346 MB peak RSS, 50,341 nodes, 224,417 edges |
| unchanged git tree | fast path, 0.13s |
| one-file edit | 1 file re-parsed, 6s (JSON load/dump plus a ~4s linear re-link) |
| `query overview` / `query stats` | ~1.7s each (loading the 188 MB graph) |
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
| graph.json | 162 MB -> 107 MB | 375 MB -> 257 MB |
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

After the same round, the Python repos: pandas 16,611 typed / 28,307 ambiguous / 0 unique (build
17s), TensorFlow 78,698 typed / 65,120 ambiguous / 0 unique (build 34s). Return-type inference
adds roughly a third to build time on Python-heavy repos.

Fixture (`skills/code-graph/tests/fixture`, 63 indexed source files across nine languages plus
build metadata such as `go.mod`, `Cargo.toml`, `package.json` and `tsconfig.json`; the suite has
176 check sites; 2026-09-16):

- Full build well under a second; incremental rebuild re-parses only changed files (1 of 63 after
  a one-line edit) and yields the same node and edge sets as a full build.
- Java example from the article reproduced: `PaymentOrchestrator` card shows injected fields and
  `processTransaction` calls resolved as `typed`; changing `PaymentRequest` lists the interface
  and class API contracts, the gateway client, the audit logger and the test.
- Skeleton of a 2,000-line Python file: ~26.9k raw tokens vs ~3.9k skeleton tokens (86% saved).

## Related agents

The `terraform`, `helm` and `kubernetes` agents declare `code-graph`, `blast-radius` and
`code-skeleton` in their `skills:` list and `build-pipeline` declares `code-graph` and
`code-skeleton`, so they answer structural questions through the same engine instead of
re-implementing parsing; the HCL/YAML extractors live here so that all platform agents share one
map. For a full architecture or impact question, delegate to this agent.
