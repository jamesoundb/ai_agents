# code-graph: usage reference

Detail moved out of SKILL.md to keep the always-loaded instructions short. SKILL.md covers the
everyday workflow; read this for build options, platform notes, naming rules, confidence labels
and the Python/C++ bridge.

## Building: performance and options
Build once from the repo root (`scripts/run.sh build --root .`). After that every `query` checks the
git working tree against the graph's stamp and refreshes a stale graph itself, with the options the
build recorded (a stderr line says so); `query --no-refresh` or `ASTGRAPH_NO_REFRESH=1` turns that
off. Outside a git checkout there is no stamp, so run `build` again after editing files. A graph built
before stamps recorded their options is not refreshed; a query says so once the engine has changed, and
one `build` turns refreshing on. In a git checkout an unchanged working tree returns in well under a second (the
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

## More query options

- `callers CLASS --no-members` keeps only edges to the class itself (instantiations, extends,
  signature references): the right view for a constructor change.
- `callers X --depth 1 --summary` on a hub: directories and the most frequent callers instead of rows.
- `trace-deps X --summary` / `--files-only`: a hub target's blast radius without per-edge rows.
- `query file PATH --no-calls --used-by --within DIR` counts only users under DIR (or a glob).
- `find` lists exact-name matches first and caps substring hits at 15 when exact matches exist
  (`--kind` drops path-only hits; `--no-tests` hides test-file symbols).
- `path` ignores `ambiguous` edges unless `--include-ambiguous`.
- `source X --max-lines N` (default 60; 0 = all): a longer function is cut with the remaining range
  named, a longer class prints its member outline with ranges instead. All names in one call share a
  150-line budget; a name past it gets its header and range only. `--refs N` names more callers.
- `Name@line` picks one overload: the definition starting on that line, else the one whose body contains
  it (`ObjectMapper.readValue@3860`); `file:Name@line` works too.
- `tests-for X` lists the test functions that reach X through up to `--depth 3` caller hops, grouped by
  test file, firm paths first; `?` marks a test reached only through an ambiguous edge (tests often call
  through untyped locals). `--no-ambiguous` keeps firm paths only, `--top N` lists more files.
- `symbol X --calls` lists each member's calls; `--all` removes every cap (and implies `--calls`).
- `overview` is the one query that weighs the whole graph (~3s/265 MB on TensorFlow); everything else
  reads through SQLite indexes in 0.2-0.4s.
- Every query accepts `--json`; `query --root DIR ...` or `query --graph PATH ...` reads a graph
  elsewhere; by default the nearest `.ast-graph/graph.db` at or above the current directory is used.
