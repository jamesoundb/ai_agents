---
name: code-graph
description: >
  Build and query a deterministic tree-sitter relationship graph of a repository (classes, functions,
  fields, calls, imports, inheritance, Terraform resources/modules, Kubernetes objects) instead of
  grepping and reading raw files. Use when you need to understand architecture, find callers or
  callees, trace dependencies, rank hub symbols, or map Terraform/Kubernetes relationships.
  Supports Python, JavaScript/TypeScript, Go, Java, Kotlin, Rust, HCL and YAML.
allowed-tools: Bash(*/code-graph/scripts/run.sh *), Bash(python3 */code-graph/scripts/astgraph.py *), Read, Glob, Grep
---

# code-graph: query a map of the code, do not dump the code

Tree-sitter parses every supported file into a symbol skeleton, and this skill links those
skeletons into a graph stored at `.ast-graph/graph.json` (relative to the repo root). The
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
   (by content hash) but every file is re-linked, which takes seconds on small repos and tens of
   seconds on 1M+ line repos:
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

Every query subcommand accepts `--json` for machine-readable output (`stats` always prints
JSON). Run `query --root DIR ...` from outside the repo (the graph is read from
`DIR/.ast-graph/graph.json`) or `query --graph PATH ...` for a graph at a custom path.

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

The linker is receiver-aware and import-aware:

- Imports bind local names to files: `from pkg import mod` binds `mod` to `pkg/mod.py`,
  `import pandas as pd` binds `pd` to `pandas/__init__.py`, and re-exports are followed up to
  three levels (`pd.DataFrame` reaches `pandas/core/frame.py`; `test.TestCase` reaches the class
  it aliases). A qualified call (`ops.convert_to_tensor(...)`) resolves only inside the bound
  files, with `import` confidence, never to a same-named function elsewhere.
- `x.f()` is resolved only when the linker knows what `x` is (a declared or inferred type, a
  field type, `self`/`this`, an import binding, or the return type of a call: `r = make()`,
  `let s = build()?`, `var g = getGateway()`, `getStyle().getNullText()`, `self.repo().save()`
  are all typed from the callee's declared return type, unwrapping `Result`/`Option`/`Promise`
  where the code does). A field typed by a generic parameter resolves through the parameter's
  bound (`sink: S` with `S: Sink` reaches `Sink.matched`), and an unbounded one yields no edge. Members are matched by the resolved type
  node and its resolved ancestors, so two classes named `TestCase` are never confused. If `x`
  belongs to an external package or is a builtin (`error`, `string`, `List`), the call is left
  unresolved. A call on a value of unknown type yields a `same_file` edge at most, otherwise up
  to three `ambiguous` leads in the same language; more candidates than that is noise, not a lead.
- An unqualified call (`f()`) resolves through a from-import binding, then the same file, then the
  same Go/Java/Kotlin package, then wildcard imports. It never targets a method (except the
  implicit `this` of Java and Kotlin), never a language builtin (`len`, `type`, `map`, `require`,
  `make`), and is never guessed by name alone.
- Base classes and signature types carry their qualifier: `collections_abc.Iterable` is external,
  `data_types.DatasetV2` resolves inside the bound module, fully-qualified names
  (`org.apache.commons.lang3.builder.Builder<T>`, `pkg.sub.Class`) resolve through the package,
  a class's own nested members are never candidates for its `extends`/`implements` clause, and a
  bare type name that is neither imported nor in scope produces no edge.
- Re-exports are followed everywhere they occur: Python `from x import y` in `__init__.py`,
  JS/TS `export { a as b } from` / `export * from` barrels, Rust `pub use` (grouped paths
  expanded).
- Kotlin: inside `fun T.f()` the receiver `this` (and `this@f`) is `T`; a top-level
  `val currentDialect: Dialect` is a typed variable node, so `currentDialect.functionProvider.f()`
  resolves through the property chain from any file that imports it (explicitly or by wildcard)
  or shares its package.
- Terraform: references are resolved within a directory (root module or one module), `module.x.y`
  reaches `output.y` in the called module's directory, and a registry source whose `//subdir`
  exists in the repo (`ns/name/google//modules/x` with a local `modules/x`) gets an `ambiguous`
  lead to that directory next to the `external` edge, so `trace-deps modules/x
  --include-ambiguous` finds the examples that exercise it.

Anything the graph could not resolve appears under "Unresolved" in a symbol card and is usually
an external library, a builtin, or generated/unindexed code.

## Rules for the model

- Never `cat` a whole file to find out what is in it. Use `query file` or the `code-skeleton`
  skill, then read the specific range.
- Refresh the graph after editing files (`build` again) before answering questions about them.
- Quote graph output (file:line, edge type, confidence) as evidence in your answer.
- If a symbol is missing, check `query stats` for the file count and language mix; the file may be
  excluded (see `DEFAULT_EXCLUDE_DIRS` in the script) or in an unsupported language.
- Graph file `.ast-graph/` is a build artifact: add it to `.gitignore`.

Schema and per-language coverage: `reference/graph-schema.md` and
`reference/languages.md`.
