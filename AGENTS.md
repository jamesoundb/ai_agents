# AGENTS.md

Instructions for AI coding agents working in this repository.

This repo is the organization's library of harness-neutral **agents** (`agents/<name>/AGENT.md`)
and **skills** (`skills/<name>/SKILL.md`, Agent Skills standard). `install.sh` renders and links
them into Claude Code, Codex, Gemini CLI, Antigravity and GitHub Copilot layouts.

**First session in this checkout?** Use the `install-agents` skill (it is pre-linked for every
harness: `.claude/skills`, `.agents/skills`, `.gemini/skills`, `.github/skills`). It asks which
harness, scope and target project, runs `install.sh`, verifies the result and explains how to
invoke the agents. If skills are unavailable in your harness, run
`skills/install-agents/scripts/install.sh --harness <name>` directly.

- Do not edit generated files (`.claude/`, `.agents/`, `.gemini/`, `.github/agents/`,
  `.github/skills/`); edit the canonical files and re-run `./install.sh`.
- Skill scripts must stay self-contained inside their skill folder and reference sibling skills
  only by relative path (`../code-graph/...`), never by harness-specific variables.
- Engine changes to `skills/code-graph/scripts/astgraph.py` must pass
  `skills/code-graph/tests/run_tests.sh` (11-language fixture in `skills/code-graph/tests/fixture/`);
  extend the fixture with a regression case for every linker fix.
- Behavioral tests for the skills themselves live in `evals/` (does the right skill fire, and is
  its output used correctly?). `python3 evals/validate.py` checks the suite structurally and runs
  in CI. Executing the cases spends model tokens: `python3 evals/run_agy.py` drives Antigravity
  (`agy`), `claude plugin eval .` drives Claude Code, from the same case files. Verify a new
  grader's assertion against real tool output before committing it -- see `evals/README.md`.
- Architecture reference lives in `.github/instructions.md`; worklogs stay local in
  `.github/troubleshooting/` (git-ignored).
- Every change goes through a merge request whose pipeline (`.gitlab-ci.yml`) must pass. Run the
  same checks locally first: `python3 tools/ci/check_repo.py`, `tools/ci/install_check.sh`,
  `tools/ci/smoke.sh` (see README "Contributing").

<!-- BEGIN managed by install.sh (agents repo); edits inside this block are overwritten -->
## AI agents and skills installed in this repository

Skills follow the Agent Skills standard (a folder with SKILL.md). Agents are personas; on
harnesses without agent files they are installed as `<name>` skills and invoked by name.

| agent | use it for |
|---|---|
| `ast-treesitter` | Code-architecture navigator over a tree-sitter code graph: how is this built, what calls or uses X, what breaks if X changes, where a change belongs. Read-only; use before refactors, impact analysis and large reviews. |
| `build-pipeline` | TeamCity pipeline engineer: reviews project settings for cost and reliability, triages failed builds with evidence, finds systemic waste in builds and agent capacity. Read-only against the server. |
| `helm` | Helm and GitOps (ArgoCD, Flux) engineer: reviews and authors charts, per-environment values and delivery objects, keeps test environments small and short-lived. Never installs releases. |
| `kubernetes` | Kubernetes and GKE engineer: troubleshoots live clusters and workloads from a cluster graph, measures cost waste, right-sizes manifests, sets namespace guardrails. Read-only against clusters. |
| `terraform` | Terraform engineer for Google Cloud: reviews, writes and refactors modules and roots, explains what a plan does and how risky it is, traces the blast radius of changes. Never applies. |

| skill | use it for |
|---|---|
| `blast-radius` | Use before a refactor, a signature, DTO, schema or infra change, or when reviewing a diff: every dependent file and symbol, the relationship, and the tests to run. |
| `bug-fix` | Use when fixing a reported bug, failing test or wrong behaviour, before changing code: a checklist of the report's cases, a failing test per case, then the cause fixed and each case proven. |
| `cluster-graph` | Use before kubectl get/describe loops on a live cluster: one read-only snapshot of every resource, CRDs included, then one query for what is broken, why, or what depends on an object. |
| `code-graph` | Use instead of grep and reading files whenever you need who calls or uses a symbol, what it calls, how A reaches B, what a class contains or what a change breaks: one query on the repo's tree- sitter code graph. |
| `code-skeleton` | Use before reading a source file longer than ~80 lines, or when asked what is in a file or directory: classes, functions, signatures and exact line ranges instead of the whole file. |
| `gke-cost-discovery` | Use when asked why a GKE cluster costs too much, to derive build size tiers, or before tuning node pools: unused vs unallocated capacity, over-requested workloads, stale test environments. |
| `helm-chart-review` | Use for Helm chart PRs and ArgoCD/Flux delivery changes: dependency pinning, values defaults and per-environment layering, GitOps hygiene, and a rendered-manifest review. |
| `install-agents` | Use when a developer asks to install, update or remove this repository's agents and skills in their coding tool (Claude Code, Codex, Gemini CLI, Antigravity, Copilot), or on a first session here. |
| `k8s-cluster-triage` | Use when a cluster or namespace misbehaves broadly (many pods failing, deploys rejected, nodes NotReady, HPAs blind): a live health sweep, shared causes first, each finding with evidence. |
| `k8s-guardrails` | Use to generate LimitRange, ResourceQuota and test-environment TTL rules for build and test namespaces from the build size tiers. |
| `k8s-manifest-review` | Use to review Kubernetes manifests for build and test workloads (YAML, helm template or kustomize output): requests vs size tiers, limits, TTLs, spot placement, images, hygiene. |
| `k8s-rightsize` | Use to right-size requests and limits of build and test workloads in Kubernetes manifests from measured usage (a gke-cost-discovery report), with a diff and evidence per container. |
| `k8s-workload-triage` | Use when one workload, pod, Job, Service or custom resource fails, restarts or is unreachable: the cause from the live cluster with quoted evidence and logs, and the fix as a manifest change. |
| `teamcity-build-triage` | Use when a TeamCity build is red or being rerun: classifies the failure with quoted evidence; a failure-class histogram over recent builds finds systemic waste. |
| `teamcity-config-review` | Use for changes under .teamcity/ or slow and expensive builds: TeamCity settings reviewed for timeouts, cleanup, triggers, concurrency, secrets, caches and pod templates. |
| `terraform-module-scaffold` | Use when asked to create or scaffold Terraform: a Google Cloud module or environment root that already follows Google's structure and the company conventions. |
| `terraform-plan-review` | Use when a Terraform plan must be approved: ranks its changes by risk for Google Cloud (data loss, IAM grants, public exposure, drift). |
| `terraform-review` | Use to review Terraform for Google Cloud (PR review, before a PR, CI gate): Google style and structure, GCP security rules and company conventions, with rule ids and file:line. |

Rules for every agent working here:

- Prefer the `code-skeleton` skill over printing whole files; read only the line ranges you need.
- For dependency or impact questions use the `code-graph` / `blast-radius` skills and cite
  `file:line`, edge type and confidence from their output.
- The graph artifact `.ast-graph/` is generated; never commit it.
<!-- END managed by install.sh -->
