# Project instructions

## Goals
- Develop several agents, each with a corresponding AGENT.md file, that anyone can bring into
  their own setup and modify as they see fit.
- Agent ideas: AST Tree-sitter agent (done, see below), terraform agent, helm agent, kubernetes
  agent, build pipeline agent, plus others as needs arise.
- Develop skills that the agents leverage to ensure repeatable results. Skills are generic
  starting points: use them as they are or adapt them to your own conventions.

## Repository layout (harness-neutral)
```
agents/<name>/AGENT.md          canonical agent: neutral frontmatter (name, description, tools, skills,
                                readonly, model) + system prompt. Single source of truth.
agents/<name>/README.md         user-facing spec (purpose, limits, adoption, measurements)
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
                                --scope user, --target DIR, --plugin (one plugin bundle per harness),
                                --uninstall; maintains AGENTS.md block
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

`--update` re-runs every install this clone made, without stored state. `detect_install()`
reads, per harness and scope, the plugin and the regular install separately (a harness can have
both, and each is refreshed):
- whether the plugin folder carries our marker with this clone's path;
- which skills are symlinks into this clone or copies marked with its path, and whether they
  are links or copies;
- which agent files carry the marker;
- whether `<skills>/<agent>/SKILL.md` exists (`--agents-as-skills`).

Per-harness folder paths live in one place, `loose_layout()` and `agent_file()`, shared with
install/uninstall. Every detected install is pre-flighted before any is written. Loose installs
are then pruned (`prune_stale`) of our files whose agent or skill no longer exists in the repo;
plugins are rebuilt whole anyway. An install that holds every agent in the repo counts as full:
its skill list becomes every skill, so new skills arrive with `--update`. Narrowed installs keep
their lists. New agents are only reported, because a full install missing a new agent cannot be
told apart from one narrowed with `--agents`.
Installs linked into another clone are skipped with the path. An agent file without the marker
is kept and named. `--update --force` takes over exactly those files (`TAKEOVER_FILES`); the
plain `--force` is cleared during an update, so skill paths keep the normal guard.

## Harness mapping (what install.sh emits)
| harness | project scope | user scope | agent representation |
|---|---|---|---|
| Claude Code | `.claude/skills/<s>`, `.claude/agents/<a>.md`, `CLAUDE.md` | `~/.claude/{skills,agents}` | subagent file (frontmatter: name, description, tools, skills, permissionMode) |
| Codex | `.agents/skills/<s>`, `AGENTS.md` | `~/.agents/skills` | persona skill `.agents/skills/<a>/SKILL.md` (`$<a>`) |
| Antigravity CLI/IDE | `.agents/skills/<s>`, `.agents/agents/<a>/agent.md` | `~/.gemini/config/{skills,agents}` | native custom agent (frontmatter: name, description, tools, mainAgent, subagent, model, commandExecutionPolicy, skills as paths) |
| Gemini CLI | `.gemini/skills/<s>`, `GEMINI.md` (`@AGENTS.md`) | `~/.agents/skills` (shared with Codex; installs before 2026-10-05 used `~/.gemini/skills` and are moved by the next install or `--update`) | persona skill |
| GitHub Copilot | `.github/skills/<s>`, `.github/agents/<a>.agent.md` | `~/.copilot/{skills,agents}` | custom agent (frontmatter: name, description, tools) |

Plugin layout (`install.sh --plugin`, one bundle named `ai-agents` per harness: `skills/` plus the
rendered agents, or persona skills where the harness has no agent files). Manifests come from
`render.py plugin-manifest`. The folder is owned through `.installed-by-ai-agents` and rebuilt
whole on every install:

| harness | user scope | project scope | manifest | discovery |
|---|---|---|---|---|
| Antigravity | `~/.gemini/config/plugins/ai-agents` | `.agents/plugins/ai-agents` | `plugin.json` | automatic, on startup |
| Claude Code | `~/.claude/skills/ai-agents` | `.claude/skills/ai-agents` | `.claude-plugin/plugin.json` | automatic as `ai-agents@skills-dir` |
| Gemini CLI | `~/.gemini/extensions/ai-agents` | none (Gemini CLI scans only the user dir) | `gemini-extension.json` | automatic |
| Copilot (VS Code) | `~/.copilot/plugins/ai-agents` | `.github/plugins/ai-agents` | bare `plugin.json` (no `$schema`) | only via the `chat.pluginLocations` setting |
| Codex | `~/plugins/ai-agents` | `plugins/ai-agents` | `.codex-plugin/plugin.json` + `.agents/plugins/marketplace.json` entry (`render.py codex-marketplace`) | install from `/plugins` |

Verified 2026-10-01 against the binaries on the development machine. Antigravity
(`agy` 1.2.13): `agy plugin validate`, and `agy agents` lists the plugin's agents in a HOME with
no loose install. A print-mode session listed all 13 plugin skills. Claude Code (2.1.286):
`claude plugin validate`, `claude plugin details ai-agents@skills-dir` reports 13 skills and 5
agents; a plugin agent's bare `skills:` names resolve to `<plugin>:<skill>`. Gemini CLI (0.62):
`gemini extensions validate`, `gemini skills list`. Copilot's format and discovery were read from
VS Code 1.139's workbench (manifest order: Copilot `plugin.json` with `$schema` ->
`.plugin/plugin.json` -> `.claude-plugin/plugin.json` -> bare `plugin.json`). Codex's format comes
from its published plugin spec. Neither Copilot nor Codex has been executed. The Antigravity docs
(embedded in `agy`) allow only `name, description, version, displayName, logo,
suggestedPrompts, disabled` in `plugin.json`; other fields are silently dropped.

Antigravity front ends do not share one binary (measured on macOS, 2026-10-01). The `agy` CLI is
whatever is on PATH. The VS Code extension spawns its own `~/.gemini/bin/agy --hub
--app_data_dir=antigravity`, downloaded only when older than the extension's target version.
IntelliJ runs JetBrains' ACP agent (`~/Library/Caches/JetBrains/<IDE>/acp-agents/antigravity-acp/
<ver>/agy_acp_server.par` + `localharness_external`, state in `~/.gemini/antigravity-acp`). Its
version (1.2.1, no update offered) is the ACP package's own; it is not comparable with `agy`'s,
which added Markdown agents in 1.1.6 and plugins in 1.1.15. That IntelliJ build reads loose
skills from `~/.gemini/config/skills` but not `config/agents` or `config/plugins`, so the loose
layout is the only one all three front ends read. `--app_data_dir` only moves session state; the
customization root stays `~/.gemini/config` (`agy --app_data_dir=<x> agents` lists the same agents
for `antigravity-cli`, `antigravity` and `antigravity-acp`). On the first macOS install the agent
chose `--harness gemini`. The `agy` CLI there listed those skills, but the IDEs did not and no
agents were installed. On Linux, agy 1.2.14 reads neither `~/.gemini/skills` nor
`~/.agents/skills` (print-mode session asked for skill names and paths, 2026-10-01). Only
`~/.gemini/config/skills` and workspace `.agents/skills` were read. Unexplained on macOS.

Folders each tool reads for user-scope skills (measured 2026-10-01, Linux): `agy`
`~/.gemini/config/skills`; Gemini CLI 0.62 `~/.gemini/skills` and `~/.agents/skills`. When both of
Gemini CLI's folders hold a skill, `~/.agents/skills` wins and every clash prints
`Skill conflict detected`. Codex: `~/.agents/skills`. `install.sh` treats `~/.agents/skills` as
shared. It never removes that folder or `~/.agents`, even empty, because developers link other
tools to it (e.g. `~/.gemini/config/skills -> ~/.agents/skills`). The pre-flight pass creates
nothing, so a refused install leaves no trace.

`--agents-as-skills` (Antigravity, regular install only) also writes each agent as
`<skills>/<agent>/SKILL.md` (`render.py skill`), for IntelliJ's Antigravity agent. The installer
detects that agent through `~/Library/Caches/JetBrains/*/acp-agents/antigravity-acp`,
`~/.cache/JetBrains/*/acp-agents/antigravity-acp` or `~/.gemini/antigravity-acp`. When found, a
regular install without the flag prints a `note`, and `--plugin` prints a `warn`.

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
GKE engineer: live-cluster troubleshooting (cluster-graph, k8s-cluster-triage, k8s-workload-triage) and
cost efficiency. Known problem: test builds/test environments on a GKE
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
| `terraform-review` | tree-sitter HCL rule engine (TF*/SEC*/CO* rules, `reference/rules.md`), configurable policy via `tfreview.json`, optional terraform fmt/validate (temp `TF_DATA_DIR`, lock file read-only or removed: writes nothing into the reviewed dir), tflint, trivy; exit 1 at `--fail-on` | `skills/terraform-review/scripts/run.sh` |
| `terraform-plan-review` | risk model over `terraform show -json` (or `plan -json` stream); stdlib only; exit 1 at `--fail-on` | `skills/terraform-plan-review/scripts/planreview.py` |
| `terraform-module-scaffold` | `templates/module` and `templates/root` rendered by `scripts/scaffold.py`; the templates define the conventions; edit them to change them | `skills/terraform-module-scaffold/scripts/scaffold.py` |
| `cluster-graph` | `kubegraph.py` (stdlib): read-only `kubectl get -o json` of every listable type (CRDs included, 12 in parallel) -> per-object health + links (owners, selectors, references, routes, HPA targets, webhooks/APIServices -> Services, PVC -> PV -> StorageClass, CR -> CRD, `missing` targets) in `~/.cache/kubegraph/<context>.db`; queries re-snapshot after 5 min; `why`/`show` map objects to the repo's YAML (`defined at file:line`, line-based scan, no YAML library) and list candidates for a `missing` reference; `waste` prices live waste with `../gke-cost-discovery/reference/pricing.json` (now also `pd_gb_month`, `lb_month`); `reach` evaluates NetworkPolicies (+ DNS, enforcing CNI); `RolloutStuck` health; Secret/ConfigMap values never stored; kubectl verbs other than get/api-resources/logs/version refused in code. Tests: `tests/test_kubegraph.py` on `tests/fixture/` (sanitized `--save` of minikube with `tests/faults.yaml` + `testenvs.yaml` planted) | `skills/cluster-graph/scripts/run.sh` |
| `k8s-cluster-triage` | procedure only: `health` sweep read top-down (not collected, cluster-scoped, system namespaces, capacity, shared-cause table), `why` on the top groups | `../cluster-graph/scripts/run.sh` |
| `k8s-workload-triage` | procedure only: `why` -> cause class -> fix in the manifest -> `why --refresh` after it is applied | `../cluster-graph/scripts/run.sh` |
| `code-graph` | build `.ast-graph/graph.db` once (queries refresh it when the git tree changed); `find`, `symbol`, `source`, `callers`, `callees`, `tests-for`, `trace-deps`, `overview`, `file`, `path`, `stats` | `skills/code-graph/scripts/run.sh` |
| `code-skeleton` | read-before-cat skeleton of files/directories with exact line ranges | `run.sh skeleton PATH...` |
| `blast-radius` | downstream impact matrix for a file, symbol, Terraform address or K8s object | `run.sh query trace-deps TARGET` |
| `bug-fix` | procedure only (no script): report -> checklist of cases -> failing test per case -> cause fixed at every site -> each case proven; navigation through the code-graph engine | `../code-graph/scripts/run.sh` |
| `install-agents` | bootstrap: harness-driven install/update/uninstall of this repo's agents and skills | `skills/install-agents/scripts/install.sh` -> `install.sh` |

`code-skeleton` and `blast-radius` call the `code-graph` engine through the relative path
`../code-graph/scripts/run.sh`, so the three skill folders must be installed side by side; the
installer always installs the skills an agent declares. SKILL.md files must not use
harness-specific variables such as `${CLAUDE_SKILL_DIR}`.

## Target stack assumptions
The agents and skills default to this stack. For a different one, adapt the policy files and
templates rather than adding vendor-neutral fallbacks.
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
  linking when the working tree is unchanged. `build` writes `.ast-graph/.gitignore` (`*`), so the
  directory ignores itself in any checkout.
- Builds hold `graph.db.lock` (pid). A second `build`, or a query that finds the graph missing or being
  rebuilt, waits for it (up to 900 s) and then takes the unchanged-tree fast path. The builder touches
  the lock every 5 s, and a lock touched in the last 30 s is live even when its process is invisible (a
  sandboxed query runs in its own pid namespace, where the hook's build does not exist); an older lock
  whose process is gone (or a zombie, or older than 2 h) is removed. This is what lets the Claude session-start hook
  (`skills/code-graph/scripts/autobuild.sh`) build in the background while the agent's first query
  simply waits. The hook builds the git checkout the session starts in (at its top level); a session
  started in a directory that is not a checkout but holds clones (a problem directory with several
  repositories) gets one graph per clone directly below it, built in parallel with the cores shared out
  (hidden directories skipped, at most `ASTGRAPH_AUTOBUILD_MAX`, default 8). Never `$HOME` or `/`. The
  launcher writes its pid into the lock before exec-ing the engine (same pid), so even an immediate query
  waits; the engine takes a lock that carries its own pid. A query from the problem directory names the
  clones that have graphs. One line of context; off with `ASTGRAPH_AUTOBUILD=0`. `install.sh` installs it
  whenever code-graph is installed (`graph_hook`, `tools/render.py hook HARNESS`, our entry only, removed
  by `--uninstall`):
  - Claude Code: `SessionStart` in `~/.claude/settings.json` (user) / `.claude/settings.local.json`
    (project) or the plugin's `hooks/hooks.json` (`${CLAUDE_PLUGIN_ROOT}`); output
    `hookSpecificOutput.additionalContext`; session directory from `CLAUDE_PROJECT_DIR`.
  - Gemini CLI (0.62): `SessionStart` (`matcher` `*`, named `ai-agents-code-graph`, timeout in ms) in
    `~/.gemini/settings.json` or the extension's `hooks/hooks.json` (`${extensionPath}`); same output as
    Claude; `GEMINI_PROJECT_DIR`. Headless runs need a trusted folder (`GEMINI_CLI_TRUST_WORKSPACE`).
  - Antigravity (agy 1.2, shared with the IDE): named entry `ai-agents-code-graph` with a `SessionStart`
    handler in `~/.gemini/config/hooks.json` or the plugin's `hooks.json` (runs in the folder holding
    hooks.json). `SessionStart` is not in the hooks guide (which lists PreToolUse, PostToolUse,
    PreInvocation, PostInvocation, Stop) but fires once per conversation; `autobuild.sh --antigravity` reads
    `workspacePaths` from stdin and answers `{"injectSteps":[{"ephemeralMessage": ...}]}`.
  - Gemini and Antigravity get no hook at project scope (their project hook files are shared and need
    trust). Codex and Copilot: none.
- Skill and agent descriptions are short "Use when ..." triggers: every installed description is
  sent with every model request; the detail lives in the bodies, which load only on use.
- Claude Code subagents: the `skills:` frontmatter field injects each listed skill's full SKILL.md into
  the subagent's context on every request (it does not restrict access). An AGENT.md may set `preload:`
  (a subset of `skills:`); `render.py claude` then emits only those in `skills:` and adds the `Skill`
  tool so the rest load on demand. Without `preload:` every skill is preloaded (unchanged behaviour).
  kubernetes preloads only `cluster-graph` (10k characters of agent + preload per request instead of
  37k), helm only `helm-chart-review` (6.7k instead of 23k). Other harnesses ignore `preload:`.
- `install.sh --harness <h> --target <repo> --uninstall` restores a clean working tree: it removes
  the harness folders, the managed AGENTS.md block (and an AGENTS.md that only held our header),
  and an import-only CLAUDE.md/GEMINI.md the install created. Verified as a round trip on a repo
  with a pre-existing AGENTS.md and .github/.
- Cost model: parsing is hash-incremental (cached parses are discarded when `astgraph.py`
  changes) and runs in a process pool over all CPUs (`--jobs`/`ASTGRAPH_JOBS`; serial fallback
  when a pool cannot start, e.g. a sandbox without a writable /dev/shm; results are consumed in
  walk order so the graph is identical to a serial build). Default worker count is physical
  cores (`default_jobs()`): hyperthreads gave no speed-up on TensorFlow and cost memory. Threads
  are not an option on CPython 3.12: tree-sitter 0.26 holds the GIL in `parse()` (8 threads ran
  0.8x serial) and the Python tree walk is 65% of extraction. Linking is incremental for calls,
  and when 1,500+ files need their calls resolved (a cold build) pass 2b runs in forked worker
  processes (`resolve_calls_parallel`, Linux only, memory-guarded: each worker is budgeted 10% of
  the parent's RSS, measured ~7%) whose records the parent replays in file order -- identical
  graph to a serial link. Passes 1/2a and every index are rebuilt in full (~6s on TensorFlow),
  but pass 2b replays a file's call edges from its `links` record in the parse cache unless its
  recorded dependencies (names looked up, files whose import bindings it followed, inheritance
  lists it read) meet what changed -- `interface_delta()` reduces an edit to the names whose
  definitions (or members) differ, so a body-only edit re-links one file. Verified by
  randomized incremental-vs-full trials (scratch harness: 420 over 12 repos, then 260 over 11
  after the parallel-link refactor; 0 differences) and `test_incremental_relink`. Any new lookup inside resolution must record its
  dependency (`_dn`/`_db`/`_parents`, or a footprinted `_memo`), or incremental builds go stale.
  A git-unchanged tree returns in ~0.1s.
  Measured on TensorFlow (20,780 indexed files, 445k nodes, 1.35M edges, C/C++ + Python, 8 CPUs):
  cold build ~73s on a 4-core/8-thread laptop (parse ~25s, at the hardware ceiling for 84s of
  single-core work; link ~26s of which call resolution ~15s in 4 workers; write ~12s), peak
  ~4.7 GB across processes, producing a 1.9 GB `graph.db`; a body-only/leaf edit rebuilds in
  ~30s (load cache 9s, link 8s, write 12s),
  an edit moving definitions in `ops.py` in ~55s (~3,400 files re-linked), at ~4.5 GB. It was
  11-13 min (775s measured) before 2026-10-01: the linker re-scanned every same-named definition per call
  (`candidates`, `lead_pick`, `owner_ids`), which is superlinear when thousands of C++ methods
  share a name. Those lookups are memoized/indexed now; keep it that way -- any new per-call scan
  over `by_name` or `by_file` inside link_graph will bring it back. **Build is the expensive
  half; querying is not.** Targeted queries (`symbol`, `callers`, `callees`, `trace-deps`,
  `file`, `find`, `path`, `stats`) each cost 0.2-0.4s and ~50 MB because they go through SQLite
  indexes; `overview` weighs the whole graph at ~3s/265 MB (~5.5s/500 MB with `--no-tests`).
  The next lever, if build time becomes painful, is incremental *linking* (today always full).
- Platform: Linux only is tested. macOS is expected to work (no bash 4+ constructs, `env bash`
  shebangs, `os.sep`/`os.path.join` throughout) but has never been run. Windows needs WSL or Git
  Bash for `run.sh`; the one known difference, `os.replace` failing while another process holds
  the database open, retries briefly -- written from documented behaviour, never exercised there.
- Query side (2026-10-02): every tool call re-sends the whole conversation, so the cost of a
  question is driven by the number of calls more than by the size of each output; the queries aim to
  *replace* calls (symbol + read + grep) rather than add to them. `query` finds the nearest
  `.ast-graph/graph.db` at or above the cwd (`find_graph`) and re-anchors cwd-relative paths
  (`G.cwd_prefix`); before answering it runs `refresh_if_stale`: the build records its options in
  `graph.db.stamp`, and a git stamp that no longer matches triggers an incremental rebuild with
  those options (note on stderr; `--no-refresh`/`ASTGRAPH_NO_REFRESH`; skipped without a git stamp).
  `symbol`, `source`, `callers` and `callees` take several names (`for_each_name`; a failing name
  raises `QueryError`, reported inline). `source` = header with range + compact callers (ambiguous
  marked `?`) + callees + numbered body (60 lines per body, 150 per call: `g.source_lines_left`).
  `tests-for` = test functions reaching a symbol within 3 caller hops, firm paths first, ambiguous ones
  marked `?`. A stamp without recorded options (pre-2026-10-02 graph) is never refreshed; the query
  prints a note once the engine hash differs. `Name@line` (no file) selects an overload by its start line or
  a line inside it; a case-exact name wins over case-insensitive matches. `narrow_matches` (shared by
  `ensure_one` and `overload_set`) prefers production code, bodies over C++ prototypes and Python `@overload`
  stubs, top level over nested; a name left with only overloads of one qname (≤ `CAP_OVERLOADS`) is expanded
  by `for_each_name` and `q_path` instead of raising. `drop_foreign_flags` removes another query
  subcommand's on/off flags (stderr note) before argparse runs. `q_path` adds `dispatch`/`override` hops from a
  method to `overrides_of(...)[1]` when its class has ≤ `CAP_DISPATCH_FANOUT` direct subtypes (the
  descendant walk is unbounded on hubs). `param_type_names` reads the whole signature (wrapped Java
  parameter lists lost their tail before, so their overrides went unseen). `callers`/`callees` default to depth 1, print compact rows,
  and report how many ambiguous edges they hid; `--no-tests` exists on callers/callees/trace-deps/
  source. SKILL.md is kept short (everyday workflow only); detail lives in `reference/usage.md`.
- Output budget: every cap lives in the `CAP_*` block at the top of `astgraph.py`, and each one
  must leave a stated way back — a flag that raises it (`skeleton --max-calls/--max-imports`,
  `symbol --limit/--all`, `trace-deps --max-rows/--files-only`, `source --max-lines/--refs`) or an
  exact line range to read. Defaults that keep cards small: member call lists only with
  `symbol --calls`, usage from test files folded into one per-file count line, uses from inside the
  class itself folded into one count, `Used by` rows grouped per file with the path printed once, and the
  constructor `calls` row hidden next to its `instantiates` row (all lifted by `--all`). A Kotlin companion
  object's members are listed under it.
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
skills/code-graph/scripts/run.sh query overview   # later queries refresh the graph themselves
```
No Node.js is required. The machine used for development has no system tree-sitter and no Node.

## Lower-environment testing
A throwaway minikube profile (`minikube start -p agents-test --driver=docker --addons=metrics-server`)
is the lower environment for anything Kubernetes: guardrail admission behaviour, the janitor,
`kubectl top`, discovery's kubectl path, cluster-graph (`skills/cluster-graph/tests/plant.sh agents-test [tests/fixture]` plants
`faults.yaml`, `faults-2.yaml`, `testenvs.yaml`, the Released PV and the stuck rollout, idempotently, and with a
directory saves the sanitized fixture; it refuses contexts that are not minikube/kind; namespaces `kg-faults`,
`kg-quota`, `kg-preview-123`, `kg-net` plus cluster-scoped `kg-*` objects). `minikube stop` removes the context
from kubeconfig; start again with `--driver=docker` (the default driver here is virtualbox). Use `kubectl --context agents-test`; never a shared or
production cluster. `minikube stop -p agents-test` between sessions; `minikube delete -p agents-test`
to remove. Starting minikube switches the current kubectl context (even with `--keep-context`, seen 2026-10-08 on
an existing profile); restore it afterwards.

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
The repo is hosted in GitLab. `.gitlab-ci.yml` only orchestrates; each job calls a script in `tools/ci/`
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
