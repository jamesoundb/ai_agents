---
name: code-graph
description: >
  Build and query a deterministic tree-sitter relationship graph of a repository (classes, functions,
  fields, calls, imports, inheritance, Terraform resources/modules, Kubernetes objects) instead of
  grepping and reading raw files. Use when you need to understand architecture, find callers or
  callees, trace dependencies, rank hub symbols, or map Terraform/Kubernetes relationships.
  Supports Python, JavaScript/TypeScript, Go, Java, Rust, HCL and YAML.
allowed-tools: Bash(*/code-graph/scripts/run.sh *), Bash(python3 */code-graph/scripts/astgraph.py *), Read, Glob, Grep
---

# code-graph: query a map of the code, do not dump the code

Tree-sitter parses every supported file into a symbol skeleton, and this skill links those
skeletons into a graph stored at `.ast-graph/graph.json` (relative to the repo root). The
language model should navigate that graph and only open raw source for the exact line ranges it
needs. Parsing is deterministic and cheap; do not ask the model to infer relationships that the
graph already contains.

Engine: `scripts/run.sh` (wraps `astgraph.py`, installs tree-sitter into a
private venv on first use, never modifies the repo).

## Workflow

1. **Build or refresh the graph** from the repo root. Incremental by file hash, so re-running is
   nearly free:
   ```bash
   scripts/run.sh build --root . 
   ```
   Options: `--include 'src/**'` / `--exclude '*_test.go'` (repeatable globs), `--full` to
   ignore the cache, `--out PATH` for a custom graph path. Terraform nested modules found in
   `.terraform/modules/modules.json` are indexed automatically.

2. **Orient** with the centrality overview before reading anything:
   ```bash
   scripts/run.sh query overview --top 15
   ```

3. **Locate**, then **inspect the card** for a symbol (members, calls with confidence,
   dependencies, dependents, unresolved externals):
   ```bash
   scripts/run.sh query find PaymentOrch
   scripts/run.sh query symbol PaymentOrchestrator
   ```

4. **Traverse** instead of grepping:
   ```bash
   scripts/run.sh query callers processTransaction --depth 3
   scripts/run.sh query callees DataService.convert_to_entity
   scripts/run.sh query path PaymentOrchestratorTest PaymentGatewayClient
   scripts/run.sh query file src/app/service.py     # skeleton + importers
   scripts/run.sh query trace-deps DataDTO           # blast radius (see blast-radius skill)
   ```

5. **Only then read source**, and only the line range the graph reported
   (`Read` with offset/limit, or `sed -n 'START,ENDp' FILE`).

All query subcommands accept `--json` for machine-readable output. Run `query --graph PATH ...`
if the graph is not at the default location.

## Naming symbols in queries

Accept, in order: a node id (`file::qname@line`), a qualified name (`DataService.save`), a plain
name, a file path, `file-suffix:name` (`store.go:MemStore`) to disambiguate, or a substring.
Terraform addresses use their native form (`aws_instance.app`, `module.vpc`, `var.region`,
`output.vpc_id`, `local.tags`, `data.aws_ami.app`). Kubernetes objects are `Kind/name`
(`Deployment/web`, `ConfigMap/web-config`).

## Reading confidence labels

Tree-sitter is syntactic, so cross-file edges are resolved by name with a recorded confidence:
`exact` (imports, HCL/K8s references), `typed` (receiver type known from a field, parameter or
local declaration), `same_file`, `package` (same directory in Java/Go), `import` (target lives in
an imported file), `unique` (only one definition with that name anywhere), `ambiguous` (several
candidates, all recorded). Treat `ambiguous` edges as leads, not facts; `trace-deps` and
`overview` exclude them unless `--include-ambiguous` is passed. Anything the graph could not
resolve appears under "Unresolved" in a symbol card and is usually an external library.

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
