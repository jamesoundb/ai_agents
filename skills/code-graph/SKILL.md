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

Tree-sitter parses every supported file and links the symbols into a graph at `.ast-graph/graph.db`
(the only thing written into the repo; add `.ast-graph/` to `.gitignore`). Ask the graph instead of
grepping, and let one query replace the grep + read round trips: every tool call re-sends the whole
conversation, so **fewer calls** matters more than smaller output.

Engine: `scripts/run.sh` (installs tree-sitter into a private venv under `~/.cache/astgraph` on
first use; Python 3.10+). Call it by its full path, one command per call, not through a shell
variable or chained after other commands: permission rules match the literal command.

## Workflow

1. **Build once** per repo, from its root: `scripts/run.sh build --root .` (seconds on most repos;
   a TensorFlow-size repo takes ~75s, so give it a 10-minute timeout and wait rather than grep).
   Do **not** rebuild before each question or after your edits: every `query` notices a changed git
   working tree and refreshes the graph itself. Queries work from any subdirectory.

2. **Ask the question in one call**, several names at once where you need several symbols:

   | You need | One call |
   |---|---|
   | who calls / uses X | `query callers X` (direct callers; `--depth N` for chains) |
   | X's code, to read or change it | `query source X` — location, callers, callees and the exact lines |
   | what X calls | `query callees X` |
   | members and dependents of a class | `query symbol X` (`--calls` adds each member's calls) |
   | what breaks if X changes | `query trace-deps X` (see the blast-radius skill) |
   | what is in a file and what matters | `query file PATH --no-calls --used-by` |
   | find a name | `query find NAME` (`--kind`, `--lang`, `--no-tests`) |
   | where to start in an unknown repo | `query overview --top 15 --no-tests` |
   | how A reaches B | `query path A B` |

   `symbol`, `source`, `callers` and `callees` take several names: `query source Query.build_filter
   Query.build_lookup` answers both in one call. `--no-tests` leaves test files out of `callers`,
   `callees`, `trace-deps` and `source`.

3. **Trust the line ranges.** `source` already printed the code; a card or caller row gives exact
   `file:line`. Read only a range the graph gave you, and do not grep for a symbol the graph has
   already located. Grep is for string literals, config keys, error messages and names the graph
   reports as unresolved.

## Reading the output

- Edges carry a confidence: `exact`, `typed`, `same_file`, `package`, `import`, `unique` are facts;
  `ambiguous` means the receiver's type is unknown and only the name matched. `callers` and
  `trace-deps` hide ambiguous edges and **say how many** they hid; add `--include-ambiguous` when
  that number matters (e.g. `self.query.setup_joins(...)` style calls). `source` lists them marked `?`.
- Large results degrade to a summary by themselves (`--summary`, `--files-only`, `--max-rows N`);
  cards cap each section (`--limit N`, `--all`) and fold usage from test files into one count line.
- An override note on `callers`/`trace-deps` means callers of the base method reach this one by
  dynamic dispatch; `symbol` lists `Overrides` / `Overridden by` (`--all` for the full list).
- Quote graph output (file:line, edge type, confidence) as evidence in your answer.
- A missing symbol: check `query stats` (file count, languages); the file may be excluded by default
  (`build`, `dist`, `vendor` ... use `--keep-dir`) or in an unsupported language.

Build options, platform notes, naming rules (`file:name`, `name@line`, Terraform and Kubernetes
addresses), what each confidence means and the Python/C++ bridge: `reference/usage.md`.
Schema, per-language coverage and linker rules: `reference/graph-schema.md`,
`reference/languages.md`, `reference/linker.md`.
