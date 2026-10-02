---
name: code-graph
description: >
  Build and query a deterministic tree-sitter relationship graph of a repository (classes, functions,
  fields, calls, imports, inheritance, Terraform resources/modules, Kubernetes objects) instead of
  grepping and reading raw files. Use when you need to understand architecture, find callers or
  callees, trace dependencies, rank hub symbols, or map Terraform/Kubernetes relationships.
  Supports C, C++, Python, JavaScript/TypeScript, Go, Java, Kotlin, Rust, HCL and YAML, and
  links Python to C++ across pybind11 bindings and TensorFlow-style op registration.
allowed-tools: Bash(*/code-graph/scripts/run.sh *), Bash(python3 */code-graph/scripts/astgraph.py *), Read, Glob, Grep
---

# code-graph: query a map of the code, do not dump the code

Tree-sitter parses every supported file into a symbol skeleton, and this skill links those
skeletons into a graph stored at `.ast-graph/graph.db` (relative to the repo root). The
language model should navigate that graph and only open raw source for the exact line ranges it
needs. Parsing is deterministic and cheap; do not ask the model to infer relationships that the
graph already contains.

Engine: `scripts/run.sh` (wraps `astgraph.py`, installs tree-sitter into a private venv under
`~/.cache/astgraph` on first use and refuses to start on a Python older than 3.10; the only thing
it writes into the repo is the graph under `.ast-graph/`).

## Workflow

1. **Build or refresh the graph** from the repo root. Always do this first; nothing watches the
   filesystem. In a git checkout an unchanged working tree returns in well under a second (the
   graph carries a git stamp in `<graph>.stamp`); otherwise only changed files are re-parsed
   (by content hash), and only the files whose call resolution the edit can affect are
   re-linked (the build line says how many). Parsing and, on Linux, call resolution run on one
   process per physical core (`--jobs N` or `ASTGRAPH_JOBS` to change it). Measured on
   TensorFlow (20,780 indexed files, 445k symbols, 1.35M edges, C/C++ and Python, 4-core laptop):
   a cold build takes ~75s and ~4.7 GB (all processes) and writes a 1.9 GB `graph.db`; after a body-only or leaf-file edit a rebuild takes ~30s (1-3 files
   re-linked; the rest is loading the cache and writing the database), after an edit that moves
   definitions in a hub file like `ops.py` ~55s (~3,400 re-linked), ~4.5 GB. On a repo
   that size give the first build a long command timeout (10 minutes) and wait for it rather
   than falling back to grep. **Querying it is cheap**: targeted commands (`symbol`, `callers`, `callees`,
   `trace-deps`, `file`, `find`, `path`, `stats`) each return in 0.2-0.4s using ~50 MB, because
   they read through SQLite indexes instead of loading the graph. `overview` is the one command
   that weighs the whole graph (~3s/265 MB; ~5.5s/500 MB with `--no-tests`). Use `--include` if
   you only care about part of the tree:
   ```bash
   scripts/run.sh build --root .
   ```
   Options: `--include 'src/**'` / `--exclude '*_test.go'` (repeatable globs), `--keep-dir build`
   to index a directory name that is excluded by default (`build`, `dist`, `target`, `vendor`,
   `node_modules`, `.terraform` ...; a Terraform repo's `build/` often holds Cloud Build configs),
   `--full` to ignore the cache and the stamp (needed after editing git-ignored source files),
   `--out PATH` for a custom graph path. Cached parses are discarded automatically when the engine file
   changes (updating the skill never leaves a stale graph behind). Terraform nested modules found in
   `.terraform/modules/modules.json` are indexed automatically. JavaScript/TypeScript: path
   aliases come from the nearest `tsconfig.json`/`jsconfig.json` (`compilerOptions.paths`,
   `baseUrl`, `extends` chains), workspace packages resolve by `package.json` name to their
   source entry (`exports`/`module`/`main` with `dist/` mapped to `src/`), and imports of assets
   (`.css`, `.svg`, `.json`, `.vue`, `?url` ...) are counted (`stats.asset_imports`) but produce no
   edge. Rust: crate roots come from Cargo.toml (`[lib]`/`[[bin]]` paths, workspace members) and
   `use other_crate::...` resolves to that crate's sources.

2. **Orient** with the centrality overview before reading anything:
   ```bash
   scripts/run.sh query overview --top 15 --no-tests
   ```
   `--no-tests` ignores usage coming from test files (otherwise assertion helpers top the list in
   repos with large suites); `--lang python` (repeatable) ranks within one language in mixed repos.

3. **Locate**, then **inspect the card** for a symbol (members, calls with confidence,
   dependencies, dependents, unresolved externals):
   ```bash
   scripts/run.sh query find PaymentOrch                  # exact-name matches first; --lang python, --kind class to narrow
   scripts/run.sh query symbol PaymentOrchestrator
   ```
   A method card also lists `Overrides` (the same-named method in an ancestor) and `Overridden by`
   (in descendants, three levels each way), which is how "which dialects render this differently"
   or "who implements this interface method" is answered.

4. **Traverse** instead of grepping:
   ```bash
   scripts/run.sh query callers processTransaction --depth 3
   scripts/run.sh query callers convert_to_tensor --depth 1 --summary   # hub: directories + most frequent callers
   scripts/run.sh query callees DataService.convert_to_entity
   scripts/run.sh query path PaymentOrchestratorTest PaymentGatewayClient
   scripts/run.sh query file src/app/service.py     # skeleton + importers (--json for nodes/refs)
   scripts/run.sh query file src/app/service.py --no-calls --used-by --no-tests   # + its symbols ranked by outside users
   scripts/run.sh query file src/app/service.py --no-calls --used-by --within src/api   # ... counting only users under src/api
   scripts/run.sh query trace-deps DataDTO           # blast radius (see blast-radius skill)
   scripts/run.sh query trace-deps ReplaceVars --summary     # hub target: directories + top dependents, no per-edge rows
   scripts/run.sh query trace-deps ReplaceVars --files-only  # just the affected files by hop
   ```
   Output is budgeted for context: `symbol` shows at most 40 rows per section (`--limit N`,
   `--all`) and summarises the rest per file; `trace-deps`, `callers` and `callees` switch to
   `--summary` by themselves above 200 rows (`--max-rows N`) and offer `--files-only`; `find`
   lists exact-name matches first and says how many more there are.

5. **Only then read source**, and only the line range the graph reported
   (`Read` with offset/limit, or `sed -n 'START,ENDp' FILE`).

`callers` and `trace-deps` on a method that overrides another print a note with the base
method and its direct-caller count, because callers that dispatch through the base reach the
override at runtime; `callers CLASS --no-members` keeps only edges to the class itself
(instantiations, extends, signature references), the right view for a constructor change;
`find` lists exact-name matches first and caps substring hits at 15 when exact matches exist
(`--kind` drops path-only hits; `--no-tests` hides test-file symbols); `path` ignores
`ambiguous` edges unless `--include-ambiguous`. Every query subcommand accepts `--json` for machine-readable
output (`stats` always prints JSON). Run `query --root DIR ...` from outside the repo (the graph is read from
`DIR/.ast-graph/graph.db`) or `query --graph PATH ...` for a graph at a custom path.

## Platform support

Developed and tested on **Linux only**. Everything below is what is known, not what is assumed:

- **Linux** — tested continuously, including concurrent builds and readers during a rebuild.
- **macOS** — expected to work and never run. The scripts avoid bash 4+ constructs (macOS ships
  bash 3.2) and use `#!/usr/bin/env bash`, and path handling goes through `os.sep`/`os.path.join`
  throughout, but no one has executed it there.
- **Windows** — `scripts/run.sh` is bash, so it needs WSL or Git Bash; `astgraph.py` itself is
  plain Python and should run under native Python. One known difference is handled blind:
  replacing the database while another process holds it open raises `PermissionError` on Windows
  but not on POSIX, so the swap retries briefly. That retry has not been exercised on Windows.

If you are the first to run this on macOS or Windows, treat a failure as expected rather than
surprising, and say so — the gap is lack of testing, not a belief that it works.

## Crossing the Python/C++ boundary

A Python call that resolves to nothing in Python is matched against **pybind11** exports, so
blast radius reaches the C++ that implements it:

```
process --calls (binding)--> _acme_core.run_engine  (bindings.cc:5)
_acme_core.run_engine --calls (same_file)--> RunEngine  (bindings.cc:4)
RunEngine --calls (typed)--> acme.Engine.Run  (engine.cc:4)
```

`PYBIND11_MODULE` becomes a `py_module`, each `m.def("name", ...)` a `py_binding`, and the
binding carries an edge to whatever it exports (`&Func`, or the calls inside an exported
lambda). TensorFlow-style registration is also indexed: `REGISTER_OP("X")` becomes an `op_def`
and `REGISTER_KERNEL_BUILDER(Name("X"), KernelClass)` an `implements` edge from the kernel to
the op. These hops are labelled `binding` so you can see which edges crossed a language
boundary.

Two rules keep it from inventing edges: a name exported by more than one extension module is
dropped rather than guessed at, and a call is never diverted to a binding when Python defines
that name itself.

**What is not bridged:** SWIG, Cython, ctypes/cffi and Boost.Python. A repo binding through
those still has its Python and C++ halves indexed separately, and a blast radius that stops at
the boundary will look complete rather than truncated — check for a binding layer before
trusting a "no dependents" answer on a C extension.

## Naming symbols in queries

Accept, in order: a node id (`file::qname@line`), a qualified name (`DataService.save`), a plain
name, a file path, `file-suffix:name` (`store.go:MemStore`) or `file-suffix:name@line`
(`readers.py:read_csv@1283`) to disambiguate, or a substring. When the suffix is also an exact
relative path it wins: `variables.tf:var.node_pools` means the root `variables.tf` even if
`modules/*/variables.tf` declare the same variable (the ambiguity message lists the ids
otherwise). A constructor (`__init__`,
`constructor`, `new`) as a `callers`/`trace-deps` target automatically includes its class, since
instantiations are recorded against the class. Overloads: calls attach to the implementation,
never to Python `@overload` stubs; among same-named Java/Kotlin/TS/Go/Rust definitions the linker picks
by argument count, then by argument types where they are known (declared parameters and
varargs, typed locals, literals, `this.`/`self.` fields, and calls typed from their return
type); when several same-arity overloads remain undecided, the call is recorded as `ambiguous`
across that overload set rather than guessed, so `--include-ambiguous` shows the candidates and
the trace note counts them.
Terraform addresses use their native form (`aws_instance.app`, `module.vpc`, `var.region`,
`output.vpc_id`, `local.tags`, `data.aws_ami.app`); `moved`/`import` blocks are `moved.<to>` /
`import.<to>` and appear as dependents of the address they target. Kubernetes objects are
`Kind/name` (`Deployment/web`, `ConfigMap/web-config`).

## Reading confidence labels

Tree-sitter is syntactic, so cross-file edges are resolved by name with a recorded confidence:
`exact` (imports, HCL/K8s references), `typed` (receiver type known from a field, parameter,
local declaration or an ancestor via `extends`/`implements`), `same_file`, `package` (same
declared package in Java/Kotlin, `src/main` and `src/test` included; same directory in Go),
`import` (target lives in an imported file, or the call is qualified with an imported repo
package/module such as `store.New()`), `unique` (only one definition with that
name anywhere), `ambiguous` (several candidates, all recorded, or a call on a receiver whose type
is unknown). Treat `ambiguous` edges as leads, not facts; `trace-deps` and `overview` exclude
them unless `--include-ambiguous` is passed.

How resolution works, in one paragraph: imports bind names to files and re-exports are followed
three levels; `x.f()` resolves only when the type of `x` is known (declared, inferred, field,
`self`/`this`, import binding or a callee's return type) and is matched by the resolved type and
its ancestors; a call on an unknown receiver gets a `same_file` edge at most, else up to three
`ambiguous` leads; an unqualified `f()` goes from-import, same file, same package, wildcard import,
and is never a builtin or guessed by name alone. Per-language details (Python typing, Kotlin
lambdas and smart casts, Terraform module paths): `reference/linker.md`.

Anything the graph could not resolve appears under "Unresolved" in a symbol card and is usually
an external library, a builtin, or generated/unindexed code.

## Rules for the model

- Never `cat` a whole file to find out what is in it. Use `query file` or the `code-skeleton`
  skill, then read the specific range.
- "What is in this file and what matters to the rest of the repo" is one call: `query file PATH
  --no-calls --used-by` ranks its symbols by how many other files use them (`--within DIR` or a
  glob for one part of the repo, `--no-tests` to ignore tests). Do not script a `callers` query
  per symbol.
- Refresh the graph after editing files (`build` again) before answering questions about them.
- Call `scripts/run.sh` by its full path, one command per call -- not through a shell variable
  (`S=.../run.sh; $S query ...`) or chained after other commands. Permission rules match the
  literal command, so the indirect forms ask for approval (or are denied in headless runs).
- Quote graph output (file:line, edge type, confidence) as evidence in your answer.
- If a symbol is missing, check `query stats` for the file count and language mix; the file may be
  excluded (see `DEFAULT_EXCLUDE_DIRS` in the script) or in an unsupported language.
- Graph file `.ast-graph/` is a build artifact: add it to `.gitignore`.

Schema, per-language coverage and linker rules: `reference/graph-schema.md`,
`reference/languages.md` and `reference/linker.md`.
