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

Not for: editing code (read-only by design), runtime/behavioral questions, questions that need a
real type checker (overloads, generics, dynamic dispatch). It states these limits explicitly.

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

Engine: `astgraph.py` (Python 3.9+, `tree-sitter`, `tree-sitter-language-pack`). `run.sh` installs
those into `~/.cache/astgraph/venv` on first use; override with `ASTGRAPH_PYTHON` or
`ASTGRAPH_VENV`. Graph artifact: `.ast-graph/graph.json` (gitignore it). Incremental by file hash.

## Languages

Python, JavaScript, TypeScript/TSX, Go, Java, Rust, Terraform/HCL, Kubernetes YAML (including
Kustomization and Helm `values.yaml`; Helm templates are recognized but not parsed). Details and
limits: [`languages.md`](../../skills/code-graph/reference/languages.md).

## Guarantees and limits

- Deterministic: same inputs, same graph. No model call is needed to build or query it.
- Every cross-file edge carries a confidence label (`exact`, `typed`, `same_file`, `package`,
  `import`, `unique`, `ambiguous`, `external`). Consumers should treat `ambiguous` as leads.
- Syntactic only: no overload/generic/dynamic-dispatch resolution; interface calls resolve to the
  interface method (follow `implements` edges for implementations).

## Adoption checklist for a repository

1. From a clone of this repo: `./install.sh --harness all --target /path/to/your/repo`
   (add `--copy` if symlinks are not an option, e.g. Windows without developer mode).
2. Add `.ast-graph/` to the repo's `.gitignore`.
3. Run `<skills dir>/code-graph/scripts/run.sh build --root .` once and check `query stats`
   shows the expected languages and file counts. Add `--exclude` globs or extend `EXT_LANG` if not.
4. Optional CI: rebuild the graph on the default branch and publish `overview --json` as an
   architecture snapshot; run `trace-deps` for each changed file on pull requests.

## Measured behaviour (fixture, 2026-09-07)

- 28-file, 8-language fixture: 151 nodes, 225 edges, build in well under a second; incremental
  rebuild re-parses only changed files (1 of 28 after a one-line edit).
- Java example from the article reproduced: `PaymentOrchestrator` card shows injected fields and
  `processTransaction` calls resolved as `typed`; changing `PaymentRequest` lists the interface
  and class API contracts, the gateway client, the audit logger and the test.
- Skeleton of a 2,000-line Python file: ~26.9k raw tokens vs ~3.9k skeleton tokens (86% saved).

## Related agents (planned)

`terraform`, `helm`, `kubernetes` and `build-pipeline` agents should call this agent (or its
skills) for structural questions instead of re-implementing parsing; the HCL/YAML extractors live
here so that all platform agents share one map.
