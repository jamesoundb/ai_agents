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
evals/<case>/                   behavioural tests for the agents and skills: prompt.md (frontmatter +
                                prompt), graders/*.md, optional case.yaml (scaffold_script, add_dirs).
                                validate.py checks them structurally; run_agy.py executes them on
                                Antigravity, `claude plugin eval .` on Claude Code
evals/fixtures/                 inputs the cases run against (Python service, Terraform root, build
                                manifests, Helm chart + ArgoCD Application, TeamCity build JSON/log)
tools/ci/                       the CI checks as plain scripts (run locally or in the pipeline):
                                check_repo.py, install_check.sh, smoke.sh, release_notes.sh
.gitlab-ci.yml                  GitLab pipeline: MR checks, and a GitLab Release per vX.Y.Z tag
.gitlab/merge_request_templates/Default.md   MR checklist
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

## Install safety (ownership)
`install.sh` only replaces or removes paths it created. Ownership is proven from the files, with no
state stored anywhere: a skill is ours when it is a symlink resolving into the repo, or a copy
containing `.installed-by-ai-agents`; a rendered agent is ours when it ends with the `MARKER`
comment line, or (installs predating the marker) when its content equals what `render.py` produces
now. A conflict aborts the run with `refusing to replace <path>`; `--force` overrides. Installs run
a pre-flight pass (`DRY=1`) over every destination first, so a conflict cannot leave a half-written
install. `--uninstall` keeps anything it does not own and logs `kept <path>`.

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
community guides on 2026-09-07 and re-checked on 2026-09-16 (Antigravity: antigravity.google/docs/skills
and /docs/cli/commands/agents: workspace `.agents/skills`, global `~/.gemini/config/{skills,agents}`;
Gemini CLI: user skills `~/.gemini/skills` or the `~/.agents/skills` alias, global context
`~/.gemini/GEMINI.md`, file name configurable via `context.fileName`); older Antigravity builds used
`.agent/skills/` and `~/.gemini/antigravity/skills/` or `~/.gemini/antigravity-cli/skills/`. Re-check
when a harness changes its layout. Antigravity `skills:`
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
| `k8s-rightsize` | requests/limits from `analyze.py --json` usage rows (name matching strips generated suffixes, then prefixes); tier cap fallback; `--lifecycle`; PyYAML re-serialisation (`run.sh` installs PyYAML into the shared venv) | `skills/k8s-rightsize/scripts/rightsize.py` |
| `k8s-manifest-review` | tier/lifecycle/hygiene rules on manifests (PyYAML or tree-sitter YAML fallback via `yamlload.py`); kubeconform/kubectl validation when available; consumed by helm and teamcity reviews as `HR-*`/`TC005-*` | `skills/k8s-manifest-review/scripts/run.sh` |
| `k8s-guardrails` | LimitRange/ResourceQuota/janitor templates rendered from `build-tiers.json`; default cpu limit capped at 4x default request (LimitRange ratio); janitor image configurable (`alpine/k8s`) with portable date parsing | `skills/k8s-guardrails/scripts/guardrails.py` |
| `helm-chart-review` | HC/HV/HG/HL rules + `helm lint`/`helm template` (parent-chart fallback when subcharts are unfetched) + rendered pass | `skills/helm-chart-review/scripts/helmreview.py` |
| `teamcity-config-review` | text rules over Kotlin DSL/XML; pod templates extracted from cloud images | `skills/teamcity-config-review/scripts/tcreview.py` |
| `teamcity-build-triage` | REST/offline failure classifier (`CLASSES` table) and recent-failure histogram | `skills/teamcity-build-triage/scripts/tctriage.py` |
| `gke-cost-discovery` | `collect.sh` (read-only gcloud/kubectl/Cloud Monitoring REST/Cloud Logging/TeamCity REST, every source optional, timeouts everywhere) + `analyze.py` (stdlib; two-bucket waste model, recommendations, tiers) + `synth.py` (demo dataset). Config: `gke-cost-discovery.env` (git-ignored) | `skills/gke-cost-discovery/scripts/` |
| `terraform-review` | tree-sitter HCL rule engine (TF*/SEC*/CO* rules, `reference/rules.md`), company policy via `tfreview.json`, optional terraform fmt/validate (temp `TF_DATA_DIR`, lock file read-only or removed: writes nothing into the reviewed dir), tflint, trivy; exit 1 at `--fail-on` | `skills/terraform-review/scripts/run.sh` |
| `terraform-plan-review` | risk model over `terraform show -json` (or `plan -json` stream); stdlib only; exit 1 at `--fail-on` | `skills/terraform-plan-review/scripts/planreview.py` |
| `terraform-module-scaffold` | `templates/module` and `templates/root` rendered by `scripts/scaffold.py`; templates are the company standard | `skills/terraform-module-scaffold/scripts/scaffold.py` |
| `code-graph` | build/refresh `.ast-graph/graph.db`; `find`, `symbol`, `callers`, `callees`, `trace-deps`, `overview`, `file`, `path`, `stats` | `skills/code-graph/scripts/run.sh` |
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
- Languages: C, C++/CUDA, Python, JavaScript, TypeScript/TSX, Go, Java, Kotlin, Rust, HCL (Terraform), YAML
  (Kubernetes manifests, Kustomization, Helm values). Coverage and limits:
  `skills/code-graph/reference/languages.md`. Schema:
  `skills/code-graph/reference/graph-schema.md`.
- Storage is SQLite (`nodes`, `edges`, `parsed`, `meta`; 8 indexes). It replaced a single JSON
  document that every query loaded in full — on TensorFlow that was 1.3 GB on disk and 5.3 GB of
  RSS per query, of which 62% was the parse cache only `build` reads. Queries reach it through
  lazy views (`NodeView`, `AdjView`, `NameView`, `LazyGraph`), so `g.nodes[id]` and `g.out[src]`
  are indexed lookups. A graph in the old format, or from an older engine, is treated as absent
  and rebuilt rather than misread.
- Graph artifact `.ast-graph/graph.db` is per-repo and incremental by file hash; the
  `graph.db.stamp` sidecar (git HEAD + status + dirty-file content) lets `build` return without
  linking when the working tree is unchanged. Add `.ast-graph/` to each consuming repo's
  `.gitignore`.
- `install.sh --harness <h> --target <repo> --uninstall` restores a clean working tree: it removes
  the harness folders, the managed AGENTS.md block (and an AGENTS.md that only held our header),
  and an import-only CLAUDE.md/GEMINI.md the install created. Verified as a round trip on a repo
  with a pre-existing AGENTS.md and .github/.
- Cost model: parsing is hash-incremental (cached parses are discarded when `astgraph.py`
  changes); linking is always full but linear. A git-unchanged tree returns in ~0.1s.
  Measured on TensorFlow (20,805 indexed files, 443k nodes, 1.43M edges, C/C++ + Python):
  cold build ~11 min at 3.2 GB RSS, producing a 1.7 GB `graph.db`. **Build is now the expensive
  half; querying is not.** Targeted queries (`symbol`, `callers`, `callees`, `trace-deps`,
  `file`, `find`, `path`, `stats`) each cost 0.2-0.4s and ~50 MB because they go through SQLite
  indexes; `overview` weighs the whole graph at ~3s/265 MB (~5.5s/500 MB with `--no-tests`).
  The next lever, if build time becomes painful, is incremental *linking* (today always full).
- Platform: Linux only is tested. macOS is expected to work (no bash 4+ constructs, `env bash`
  shebangs, `os.sep`/`os.path.join` throughout) but has never been run. Windows needs WSL or Git
  Bash for `run.sh`; the one known difference, `os.replace` failing while another process holds
  the database open, retries briefly -- written from documented behaviour, never exercised there.
- Output budget: every cap lives in the `CAP_*` block at the top of `astgraph.py`, and each one
  must leave a stated way back — a flag that raises it (`skeleton --max-calls/--max-imports`,
  `symbol --limit/--all`, `trace-deps --max-rows/--files-only`) or an exact line range to read.
  `skeleton` reports what it hid once per run (`-- elided: N calls (raise with --max-calls)`),
  not per line, and applies a never-worse guard: a file no larger than its own skeleton is
  printed as source instead, and a run that saves nothing says so rather than reporting a
  negative percentage.
- Engine regression tests: `skills/code-graph/tests/run_tests.sh` copies the eleven-language fixture
  (C/C++, Python, JavaScript, TypeScript, Go, Java, Kotlin, Rust, HCL, Kubernetes YAML) from
  `skills/code-graph/tests/fixture/` to a temp dir, builds it there and asserts resolution
  confidence per language, overload and return-type choices, test-file detection, query output
  shapes (`path` text and `--json`, method-card overrides, `file:name` root preference, overview
  de-duplication, `--root`), Terraform `dynamic`/`moved`/registry-lead handling, `--keep-dir`,
  the parse-error line, the skeleton output budget (never-worse guard, `(+N more)` elisions, the
  once-per-run recovery notice, `--max-calls`/`--max-imports` including `0` = no cap),
  incremental == full, determinism across hash seeds, the git fast path and
  the engine-hash cache key. Run it
  after any change to `astgraph.py` and add a regression case for every linker fix.
- Adding a language: map the extension in `EXT_LANG`, add a handler dict keyed by tree-sitter node
  type, register it in `HANDLERS`, and extend `resolve_import` if the language has imports.

## Setup on a new machine
```bash
./install.sh --harness antigravity  # or claude|codex|gemini|copilot|all; --target DIR for another repo
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
cases, loops/comprehensions/`with`/walrus over typed collections and a pytest `conftest.py`
fixture, TS/JS with a workspace package and tsconfig `extends`,
Go with `go.mod`, Kotlin in a Gradle `src/main` + `src/test` layout with extension functions,
overrides, an imported top-level property, DSL/`also`/collection lambdas, operators, a builder
`= apply { }` chain, a nested interface of an imported type, smart casts, `by lazy`, callable
references, `operator fun invoke`, a `fun interface` and a Compose-style `@Composable`
function-type parameter, a Rust Cargo
workspace, Terraform with a root calling a local module, a registry source mirroring a local
directory, `dynamic` blocks and a `moved` block, Kubernetes manifests with
Service/Deployment/ConfigMap/HPA/Ingress, a Helm template and values file). The runner copies it
to a temp dir and never writes into the repo. The fixture contents and measured behaviour are
described in `agents/ast-treesitter/README.md`; the Terraform and Kubernetes skills describe
their own fixtures in their READMEs.

Behavioural coverage lives in `evals/` and answers a different question from the engine tests:
does a model reading a `SKILL.md` reach the right tool, and does a persona follow its `AGENT.md`?
Two families, separated by one frontmatter field:

- **skill cases** omit `agent:`; they load no persona, so they measure `SKILL.md` alone.
- **agent cases** (`agent-*`) set `agent: <name>`, which makes `run_agy.py` pass `--agent`. One
  per persona, two for `ast-treesitter` since it is the one offered to developers first: a
  Python direct lookup (fast path), a Kotlin lookup past a same-named method on an unrelated
  class, and a Python lookup whose callers are reachable only through a re-export alias and a
  factory's return annotation (the last two measure resolution, which text search gets wrong);
  then `terraform` (merge gate),
  `kubernetes` (right-size without applying), `helm` (fix the environment values layer, not the
  chart defaults), `build-pipeline` (name the failure class instead of rerunning).

Three of the agent prompts end with an instruction that breaks the persona's hard limits, so the
guardrail is measured under pressure rather than assumed. Every agent case asserts the
"Evidence / Limits" closing, which appears only in `AGENT.md` files -- it is the canary for a case
silently running without `--agent`, the failure that made the suite look like it covered the
agents when it covered only the skills.

`python3 evals/validate.py` is free and runs in CI; executing cases spends model tokens and is a
deliberate, local step (`--runs 3`: single runs have reported green suites containing defective
graders, and token counts vary ~1.5x run to run). Agents are *rendered copies*, not symlinks, so
re-run `./install.sh --harness antigravity --scope user --force` after editing an `AGENT.md` or
the measurement tests the old prompt.

## CI and releases
The repo is hosted in GitLab (company project; a personal GitLab project is the lower environment
for pipeline changes). `.gitlab-ci.yml` only orchestrates; each job calls a script in `tools/ci/`
so the same check runs on a laptop.

- Pipelines: merge request pipelines, `main`, branches without an open MR, and tags
  (`workflow:rules`; a branch with an open MR runs only its MR pipeline).
- `check` stage: `repo-checks` (`check_repo.py`: Agent Skills name/description rules, name == folder,
  agent `skills:`/`tools:` references, AGENTS.md managed block equals `render.py agents-md`),
  `lint` (blocking: ruff `E9,F63,F7,F82`, `shellcheck --severity=error`), `lint-advisory`
  (`allow_failure`: ruff `E,F,W,B` minus `E501`/`E702`/`E741` - deliberate style here, and 900+
  findings would make the job permanently yellow - plus full shellcheck. Both are expected to pass:
  the substantive findings were fixed, so a new one means new code. Lint tool versions are pinned.
- `test` stage: `engine-tests` (`run_tests.sh`), `install-check` (project scope into a temp repo +
  uninstall must leave it clean; user scope into a temp `HOME`; expected counts derived from
  `agents/` and `skills/`), `smoke` (every skill script `--help`; code-graph skeleton;
  terraform-review and k8s-manifest-review on the fixture with `--no-tools`; a freshly scaffolded
  module must pass terraform-review with no findings; where terraform is installed, a tools-mode
  review must leave that module's directory unchanged; synth -> analyze -> guardrails).
- Image `$PYTHON_IMAGE` = `mirror.gcr.io/library/python:3.12` (Google's Docker Hub mirror, same digest as
  Docker Hub's `python:3.12`); tree-sitter (`requirements.txt`) is pip-installed into the image's
  python3, which the skills' `run.sh` discovers. PyYAML is not pre-installed on purpose:
  `k8s-rightsize/scripts/run.sh` installs it, exercising the first-run path. Review scripts exit 0 = gate pass,
  1 = gate fail, 2 = error; the smoke test accepts 0/1 plus a `Gate (` line.
- Release: tags matching `^v\d+\.\d+\.\d+(-suffix)?$` run `release-notes`
  (`release_notes.sh`, full history via `GIT_DEPTH: 0`) and `release`
  (`registry.gitlab.com/gitlab-org/cli`, `glab release create` with `GLAB_ENABLE_CI_AUTOLOGIN`, i.e.
  the job token). `release` needs every check job, so a tag whose checks fail is not released.
  Creating the tag in the UI with release notes pre-creates the release and fails the job.
- Rollback policy: developers track `main`; roll back with a revert on `main` (forward fix), never
  by moving tags. Tags are known-good pins (`git checkout vX.Y.Z` + re-run install).
- Project settings the YAML cannot set: protect `v*` tags (Maintainers), "Pipelines must succeed"
  on merge, and runners/compute minutes for the group.

## Design references
- Whitney, "Stop Dumping Raw Code into LLMs: Why ASTs lead to Scalable AI Agents" (Medium).
- Hacker News 47367129 (tree-sitter skeletons, LSP centrality overview, code-map caching).
- intellectronica gist on Copilot Chat OSS, §3.1 context gathering mechanisms.
