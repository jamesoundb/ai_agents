# Project instructions

## Goals
- Develop several agents with a corresponding AGENT.md file for use across the organization.
- Agent ideas: AST Tree-sitter agent (done, see below), terraform agent, helm agent, kubernetes
  agent, build pipeline agent, plus others suggested through architectural conversations and
  company needs.
- Develop skills that the agents leverage to ensure repeatable results. Skills are tailored to
  company needs.

## Repository layout (harness-neutral)
```
agents/<name>/AGENT.md          canonical agent: neutral frontmatter (name, description, tools, skills,
                                readonly, model) + system prompt. Single source of truth.
agents/<name>/README.md         organization-facing spec (purpose, limits, adoption, measurements)
skills/<skill>/SKILL.md         Agent Skills standard; scripts/ and reference/ live beside it
tools/render.py                 renders AGENT.md -> Claude / Copilot / Antigravity agents, agent-as-skill,
                                and the managed block for AGENTS.md (stdlib only)
install.sh                      installs into a harness: symlink (default) or --copy, project or
                                --scope user, --target DIR, --uninstall; maintains AGENTS.md block
AGENTS.md                       repo instructions read by Codex, Gemini CLI, Copilot, Cursor
CLAUDE.md, GEMINI.md            "@AGENTS.md" import so Claude Code / Gemini read the same instructions
skills/install-agents/          bootstrap skill: the harness installs the agents for the developer by
                                running install.sh (wrapper in scripts/ resolves the symlinked path)
.claude/skills/install-agents, .agents/skills/install-agents, .gemini/skills/install-agents,
.github/skills/install-agents   COMMITTED symlinks -> ../../skills/install-agents so a fresh clone
                                exposes the bootstrap skill to every harness
.github/instructions.md         this file (architecture reference, not a worklog)
.github/troubleshooting/        local worklogs (git-ignored; not part of the published repo)
```
Generated, git-ignored: everything under `.claude/`, `.agents/`, `.gemini/`, `.github/agents/`,
`.github/skills/` except the four `install-agents` bootstrap symlinks (see `.gitignore`, which uses
`dir/*` + `!dir/install-agents` negations), plus `.ast-graph/`. `install.sh` never installs
`install-agents` into target projects unless named with `--skills`. Never edit generated files;
edit the canonical ones and re-run the installer (or the `install-agents` skill).

## Harness mapping (what install.sh emits)
| harness | project scope | user scope | agent representation |
|---|---|---|---|
| Claude Code | `.claude/skills/<s>`, `.claude/agents/<a>.md`, `CLAUDE.md` | `~/.claude/{skills,agents}` | subagent file (frontmatter: name, description, tools, skills, permissionMode) |
| Codex | `.agents/skills/<s>`, `AGENTS.md` | `~/.agents/skills` | persona skill `.agents/skills/<a>/SKILL.md` (`$<a>`) |
| Antigravity CLI/IDE | `.agents/skills/<s>`, `.agents/agents/<a>/agent.md` | `~/.gemini/config/{skills,agents}` | native custom agent (frontmatter: name, description, tools, mainAgent, subagent, model, commandExecutionPolicy, skills as paths) |
| Gemini CLI | `.gemini/skills/<s>`, `GEMINI.md` (`@AGENTS.md`) | `~/.gemini/skills` | persona skill |
| GitHub Copilot | `.github/skills/<s>`, `.github/agents/<a>.agent.md` | `~/.copilot/{skills,agents}` | custom agent (frontmatter: name, description, tools) |

Neutral tool names in AGENT.md (`shell, read, glob, grep, edit, write, web, search`) are mapped per
harness in `tools/render.py` (`TOOL_MAP`). Vendor locations were verified against vendor docs and
community guides on 2026-09-07 (Antigravity: antigravity.google/docs/subagents and
/docs/cli/commands/agents); older Antigravity builds used `.agent/skills/` and
`~/.gemini/antigravity/skills/`. Re-check when a harness changes its layout. Antigravity `skills:`
entries are paths, so the renderer receives the installed skills prefix from install.sh.

## Agents

### ast-treesitter
Read-only code-architecture navigator built on tree-sitter. Builds a semantic relationship graph
(files, classes, functions, fields, calls, imports, inheritance, Terraform resources/modules,
Kubernetes objects) and answers structure, dependency and impact questions from the graph rather
than from raw file dumps. Canonical: `agents/ast-treesitter/AGENT.md`; spec:
`agents/ast-treesitter/README.md`.

### terraform
Terraform engineer for Google Cloud (target stack: GCP, GCS state, Helm via GitOps for K8s).
Reviews/writes/refactors Terraform, reviews plans as risk, scaffolds compliant code, uses the HCL
code graph for blast radius. Hard limits: never apply/destroy/state-write; no secrets in code.
Canonical: `agents/terraform/AGENT.md`; spec: `agents/terraform/README.md`.

### kubernetes
GKE engineer, cost-efficiency first. Known problem: test builds/test environments on a GKE
Standard cluster show unused + unallocated capacity in billing despite autoscaling; developers
edit TeamCity build templates (manifests, replica counts). Read-only against clusters; changes
go to manifests/Helm/Terraform. Inputs to configure: project, cluster, location, build
namespaces, read-only credentials, CI server URL/token, a real build manifest.
Canonical: `agents/kubernetes/AGENT.md`; spec: `agents/kubernetes/README.md`.

### helm
Helm/GitOps engineer (ArgoCD or Flux). Chart + values + delivery-object review, renders and
checks against build tiers; never installs/upgrades. Canonical: `agents/helm/AGENT.md`.

### build-pipeline
TeamCity engineer (tests and builds run there; build templates size the GKE pods). Settings
review, failure triage by class, CI health/cost review; read-only against the server.
Canonical: `agents/build-pipeline/AGENT.md`.

## Skills

| skill | purpose | entry point |
|---|---|---|
| `k8s-rightsize` | requests/limits from `analyze.py --json` usage rows (name matching strips generated suffixes, then prefixes); tier cap fallback; `--lifecycle`; PyYAML re-serialisation | `skills/k8s-rightsize/scripts/rightsize.py` |
| `k8s-manifest-review` | tier/lifecycle/hygiene rules on manifests (PyYAML or tree-sitter YAML fallback via `yamlload.py`); kubeconform/kubectl validation when available; consumed by helm and teamcity reviews as `HR-*`/`TC005-*` | `skills/k8s-manifest-review/scripts/run.sh` |
| `k8s-guardrails` | LimitRange/ResourceQuota/janitor templates rendered from `build-tiers.json`; default cpu limit capped at 4x default request (LimitRange ratio); janitor image configurable (`alpine/k8s`) with portable date parsing | `skills/k8s-guardrails/scripts/guardrails.py` |
| `helm-chart-review` | HC/HV/HG/HL rules + `helm lint`/`helm template` (parent-chart fallback when subcharts are unfetched) + rendered pass | `skills/helm-chart-review/scripts/helmreview.py` |
| `teamcity-config-review` | text rules over Kotlin DSL/XML; pod templates extracted from cloud images | `skills/teamcity-config-review/scripts/tcreview.py` |
| `teamcity-build-triage` | REST/offline failure classifier (`CLASSES` table) and recent-failure histogram | `skills/teamcity-build-triage/scripts/tctriage.py` |
| `gke-cost-discovery` | `collect.sh` (read-only gcloud/kubectl/Cloud Monitoring REST/Cloud Logging/TeamCity REST, every source optional, timeouts everywhere) + `analyze.py` (stdlib; two-bucket waste model, recommendations, tiers) + `synth.py` (demo dataset). Config: `gke-cost-discovery.env` (git-ignored) | `skills/gke-cost-discovery/scripts/` |
| `terraform-review` | tree-sitter HCL rule engine (TF*/SEC*/CO* rules, `reference/rules.md`), company policy via `tfreview.json`, optional terraform fmt/validate, tflint, trivy; exit 1 at `--fail-on` | `skills/terraform-review/scripts/run.sh` |
| `terraform-plan-review` | risk model over `terraform show -json` (or `plan -json` stream); stdlib only; exit 1 at `--fail-on` | `skills/terraform-plan-review/scripts/planreview.py` |
| `terraform-module-scaffold` | `templates/module` and `templates/root` rendered by `scripts/scaffold.py`; templates are the company standard | `skills/terraform-module-scaffold/scripts/scaffold.py` |
| `code-graph` | build/refresh `.ast-graph/graph.json`; `find`, `symbol`, `callers`, `callees`, `trace-deps`, `overview`, `file`, `path`, `stats` | `skills/code-graph/scripts/run.sh` |
| `code-skeleton` | read-before-cat skeleton of files/directories with exact line ranges | `run.sh skeleton PATH...` |
| `blast-radius` | downstream impact matrix for a file, symbol, Terraform address or K8s object | `run.sh query trace-deps TARGET` |
| `install-agents` | bootstrap: harness-driven install/update/uninstall of this repo's agents and skills | `skills/install-agents/scripts/install.sh` -> `install.sh` |

`code-skeleton` and `blast-radius` call the `code-graph` engine through the relative path
`../code-graph/scripts/run.sh`, so the three skill folders must be installed side by side; the
installer always installs the skills an agent declares. SKILL.md files must not use
harness-specific variables such as `${CLAUDE_SKILL_DIR}`.

## Target stack assumptions
The agents and skills are tailored to this stack; adjust the policy files and templates for a
different one rather than adding vendor-neutral fallbacks.
- Cloud: Google Cloud. Terraform state: GCS buckets (no Terraform Enterprise/Cloud assumed).
- CI: TeamCity for tests and builds (build-pipeline agent). The Terraform agent makes no CI
  assumptions (its scripts just exit non-zero at the gate).
- Kubernetes delivery: Helm charts through GitOps (ArgoCD or Flux). kube-janitor for TTL cleanup
  (annotations `janitor/ttl`, `janitor/expires`, rules file); the standalone CronJob janitor is
  opt-in only for clusters without it.
- Test builds on a GKE Standard cluster, sized by CI build templates; the kubernetes agent owns
  cost efficiency there.

## Engine: astgraph.py
- Location: `skills/code-graph/scripts/astgraph.py` (single file, Python 3.10+; developed and
  tested on 3.12).
- Dependencies: `tree-sitter>=0.24`, `tree-sitter-language-pack>=1.0`
  (`scripts/requirements.txt`); those floors are the first releases that require Python 3.10,
  so pip refuses to install on 3.9 rather than resolving old untested versions. `run.sh` finds a
  Python that has them or creates `~/.cache/astgraph/venv` on first use from `python3` (or
  `ASTGRAPH_PYTHON`), after checking that interpreter is 3.10+ (exit 2 with a message
  otherwise). Override with `ASTGRAPH_PYTHON=/path/to/python` or `ASTGRAPH_VENV=/path/to/venv`.
- Languages: Python, JavaScript, TypeScript/TSX, Go, Java, Kotlin, Rust, HCL (Terraform), YAML
  (Kubernetes manifests, Kustomization, Helm values). Coverage and limits:
  `skills/code-graph/reference/languages.md`. Schema:
  `skills/code-graph/reference/graph-schema.md`.
- Graph artifact `.ast-graph/graph.json` is per-repo and incremental by file hash; the
  `graph.json.stamp` sidecar (git HEAD + status + dirty-file content) lets `build` return without
  linking when the working tree is unchanged. Add `.ast-graph/` to each consuming repo's
  `.gitignore`.
- `install.sh --harness <h> --target <repo> --uninstall` restores a clean working tree: it removes
  the harness folders, the managed AGENTS.md block (and an AGENTS.md that only held our header),
  and an import-only CLAUDE.md/GEMINI.md the install created. Verified as a round trip on a repo
  with a pre-existing AGENTS.md and .github/.
- Cost model: parsing is hash-incremental (cached parses are discarded when `astgraph.py`
  changes); linking is always full but linear (~4s per 3.6k
  files); graph load/dump is proportional to graph size (~1-3s per 100-300 MB). A git-unchanged
  tree returns in ~0.1s. Query output is capped (`symbol --limit`, `trace-deps --max-rows`).
- Engine regression tests: `skills/code-graph/tests/run_tests.sh` copies the nine-language fixture
  (Python, JavaScript, TypeScript, Go, Java, Kotlin, Rust, HCL, Kubernetes YAML) from
  `skills/code-graph/tests/fixture/` to a temp dir, builds it there and asserts resolution
  confidence per language, overload and return-type choices, test-file detection, query output
  shapes (`path` text and `--json`, method-card overrides, `file:name` root preference, overview
  de-duplication, `--root`), Terraform `dynamic`/`moved`/registry-lead handling, `--keep-dir`,
  the parse-error line, incremental == full, determinism across hash seeds, the git fast path and
  the engine-hash cache key. Run it
  after any change to `astgraph.py` and add a regression case for every linker fix.
- Adding a language: map the extension in `EXT_LANG`, add a handler dict keyed by tree-sitter node
  type, register it in `HANDLERS`, and extend `resolve_import` if the language has imports.

## Setup on a new machine
```bash
./install.sh --harness all        # or claude|codex|gemini|antigravity|copilot; --target DIR for another repo
skills/code-graph/scripts/run.sh build --root .   # first run creates ~/.cache/astgraph/venv
skills/code-graph/scripts/run.sh query overview
```
No Node.js is required. The machine used for development has no system tree-sitter and no Node.

## Lower-environment testing
A throwaway minikube profile (`minikube start -p agents-test --driver=docker --addons=metrics-server`)
is the lower environment for anything Kubernetes: guardrail admission behaviour, the janitor,
`kubectl top`, discovery's kubectl path. Use `kubectl --context agents-test`; never a shared or
production cluster. `minikube stop -p agents-test` between sessions; `minikube delete -p agents-test`
to remove. Starting minikube switches the current kubectl context; restore it afterwards.

## Testing convention
Terraform skills are verified against a scratch fixture (root + modules with planted violations,
a clean module, a real `terraform show -json` plan produced offline with the google provider, a
synthetic risky plan) and the scaffold output must pass `terraform validate` and the review gate;
fixtures are described in the skills' READMEs.
Engine changes are verified against the committed fixture in `skills/code-graph/tests/fixture/`
(Java example from the source article, Python with re-exporting packages, `Optional`/enum/alias
cases and a pytest `conftest.py` fixture, TS/JS with a workspace package and tsconfig `extends`,
Go with `go.mod`, Kotlin in a Gradle `src/main` + `src/test` layout with extension functions,
overrides, an imported top-level property, DSL/`also`/collection lambdas, operators, a builder
`= apply { }` chain and a nested interface of an imported type, a Rust Cargo
workspace, Terraform with a root calling a local module, a registry source mirroring a local
directory, `dynamic` blocks and a `moved` block, Kubernetes manifests with
Service/Deployment/ConfigMap/HPA/Ingress, a Helm template and values file). The runner copies it
to a temp dir and never writes into the repo. The fixture contents and measured behaviour are
described in `agents/ast-treesitter/README.md`; the Terraform and Kubernetes skills describe
their own fixtures in their READMEs.

## Design references
- Whitney, "Stop Dumping Raw Code into LLMs: Why ASTs lead to Scalable AI Agents" (Medium).
- Hacker News 47367129 (tree-sitter skeletons, LSP centrality overview, code-map caching).
- intellectronica gist on Copilot Chat OSS, §3.1 context gathering mechanisms.
