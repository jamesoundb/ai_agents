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
- Engine changes to `skills/code-graph/scripts/astgraph.py` must be verified against a
  multi-language fixture (see `agents/ast-treesitter/README.md` for what it covers).
- Architecture reference lives in `.github/instructions.md`; worklogs stay local in
  `.github/troubleshooting/` (git-ignored).

<!-- BEGIN managed by install.sh (agents repo); edits inside this block are overwritten -->
## AI agents and skills installed in this repository

Skills follow the Agent Skills standard (a folder with SKILL.md). Agents are personas; on
harnesses without agent files they are installed as `<name>` skills and invoked by name.

| agent | use it for |
|---|---|
| `ast-treesitter` | Code-architecture navigator. Uses tree-sitter skeletons and a semantic relationship graph (classes, functions, fields, calls, imports, inheritance, Terraform modules/resources, Kubernetes objects) to answer "how is this built", "what calls/uses X", "what breaks if I change X", and "where should this change go" without dumping raw files into context. |
| `build-pipeline` | TeamCity build pipeline engineer with a cost and reliability mandate. Reviews TeamCity project settings (Kotlin DSL or XML) for timeouts, cleanup, triggers, concurrency, secrets, caches and Kubernetes cloud-image pod templates against the build size tiers; triages failed builds by class with evidence; finds systemic waste (infrastructure failures that get rerun, queue time, test environments left running by builds). |
| `helm` | Helm and GitOps engineer for Kubernetes workloads delivered through ArgoCD or Flux. Reviews and authors charts, values layering per environment, ArgoCD Applications and Flux HelmReleases; keeps test environments small and short-lived; renders charts and checks the output against the company's build size tiers. |
| `kubernetes` | Kubernetes and GKE engineer with a cost-efficiency mandate. Measures where a GKE Standard cluster wastes money (unused vs unallocated capacity), right-sizes build and test-environment manifests from real usage, proposes build size tiers and namespace guardrails (LimitRange, ResourceQuota), tunes node pools and the cluster autoscaler, and reviews manifests and Helm output for correctness. |
| `terraform` | Terraform engineer for Google Cloud. Reviews, writes and refactors Terraform (modules and environment roots) against Google's best practices and company policy, explains what a plan will do and how risky it is, scaffolds compliant modules, and traces the blast radius of variable/module/output changes through the code graph. |

| skill | use it for |
|---|---|
| `blast-radius` | Compute the downstream impact of changing a file, class, function, DTO, Terraform resource or module, or Kubernetes object: which files and symbols depend on it, through which relationship, and which tests to run. Use before refactors, signature changes, schema/DTO changes, infra changes, and when reviewing a diff for unintended consequences. |
| `code-graph` | Build and query a deterministic tree-sitter relationship graph of a repository (classes, functions, fields, calls, imports, inheritance, Terraform resources/modules, Kubernetes objects) instead of grepping and reading raw files. Use when you need to understand architecture, find callers or callees, trace dependencies, rank hub symbols, or map Terraform/Kubernetes relationships. |
| `code-skeleton` | Show a token-light tree-sitter skeleton of one or more source files (classes, functions, fields, signatures, decorators, calls, exact line ranges) before reading any raw code. Use proactively instead of cat/Read on any file longer than ~80 lines, and whenever asked "what is in this file/directory". |
| `gke-cost-discovery` | Measure where a GKE Standard cluster wastes money on test builds and test environments: unused capacity (requested but not consumed) versus unallocated capacity (nodes idle because nothing requested them), top over-requested workloads with recommended requests, stale test environments, lingering Jobs, scale-down blockers, node pool settings, autoscaler no-scale-down reasons, and TeamCity build demand. Use when asked why the cluster costs too much, to right-size build manifests, to derive build size tiers, or before tuning node pools. |
| `helm-chart-review` | Review Helm charts and their GitOps delivery objects together: Chart.yaml and dependency pinning, values.yaml defaults (resources, image tag, pull policy, secrets), per-environment values layering and effective replica/resource settings, dead values, ArgoCD Application and Flux HelmRelease hygiene (pinned revisions, projects, prune/selfHeal, TTL for test environments, remediation), plus helm lint and a rendered-manifest pass through k8s-manifest-review. Use for chart PRs, GitOps changes, "why is this environment so big", and before promoting a chart version. |
| `install-agents` | Install or update this repository's AI agents and skills into the developer's coding harness (Claude Code, OpenAI Codex, Gemini CLI, Google Antigravity, GitHub Copilot), for this checkout or for another project on disk. Use when a developer asks to install, set up, enable, update or remove the agents/skills, asks how to use them in their tool, or on a first session in this repository. |
| `k8s-guardrails` | Generate namespace guardrails for build and test namespaces from the company's build size tiers: a LimitRange (defaults and per-container maximums, memory limit = request), a ResourceQuota sized for expected concurrency, and a kube-janitor rules entry that gives unannotated test environments a default TTL (a standalone janitor CronJob is available only for clusters without kube-janitor). Use after gke-cost-discovery has produced build-tiers.json, when a namespace has no LimitRange, or when test environments are left running. |
| `k8s-manifest-review` | Efficiency and hygiene review of Kubernetes manifests for build and test workloads on GKE: missing or oversized requests versus the company's build size tiers, memory limits, lifecycle (Job TTL and deadlines, test-environment teardown TTL, replica caps), spot pool placement, ephemeral storage, image pinning, sidecars, PVCs, privileged containers and labels. Works on plain YAML, `helm template` output and `kustomize build` output. |
| `k8s-rightsize` | Right-size the requests and limits of build and test workloads in a namespace's manifests from measured usage: matches each container to the gke-cost-discovery report (p95 CPU and memory), rewrites requests to p95 x headroom with memory limit = request and a bounded CPU limit, caps or defaults to the build size tier when there is no usage data, optionally adds the lifecycle fields (Job TTL, deadline, backoffLimit, kube-janitor TTL annotation), and prints a diff with the evidence per container. Use after a discovery run, when a namespace shows low utilization, or when asked to "right-size this job/manifest/namespace". |
| `teamcity-build-triage` | Explain why a TeamCity build failed and what to do: classifies the failure (agent/pod scheduling, OOM or disk or timeout, dependency/registry, compile, tests, configuration, flaky suspect) from the build log, build problems and failed tests, with quoted evidence lines; also produces a failure-class histogram over recent failed builds to find systemic waste (builds that fail for infrastructure reasons and get rerun). Use when a build is red, when developers rerun builds "to see if it passes", and for weekly CI health reviews. |
| `teamcity-config-review` | Review TeamCity project settings (Kotlin DSL settings.kts/*.kt or XML project-config and buildTypes) for cost and hygiene: execution timeouts, cleanup rules, VCS trigger branch filters and quiet periods, concurrency caps, artifact rules, plain-text secrets, unpinned step images, build caches, builds that create Kubernetes test environments without a teardown, and Kubernetes cloud-image pod templates reviewed against the build size tiers. Use for changes under .teamcity/, when builds are slow or expensive, and before adding a build configuration. |
| `terraform-module-scaffold` | Generate a new Terraform module or environment root module for Google Cloud that already follows Google's standard structure and the company conventions (versions pinned, GCS backend with prefix, labels, described and typed variables, outputs, README with terraform-docs markers, runnable example). Use when asked to create, start, bootstrap or scaffold Terraform code. |
| `terraform-plan-review` | Turn a Terraform plan into a risk-ranked change review for Google Cloud: destroys and replacements of stateful resources (data loss), resources removed from config, IAM grants (public members, primitive roles, authoritative policies), internet-open firewalls, public IPs, sensitive outputs, drift, and metadata-only noise. Use whenever a plan must be approved, in PR or CI pipelines, and before any apply. |
| `terraform-review` | Deterministic review of Terraform for Google Cloud: Google's style/structure rules (variables, outputs, versions, backend, module pinning, naming), GCP security rules (public IAM, primitive roles, service account keys, open firewalls, public Cloud SQL, GKE hardening, bucket settings, secrets in code) and company conventions (required labels, allowed regions). Use for PR review, before opening a PR, when asked "is this Terraform safe/compliant", and as a CI gate. |

Rules for every agent working here:

- Prefer the `code-skeleton` skill over printing whole files; read only the line ranges you need.
- For dependency or impact questions use the `code-graph` / `blast-radius` skills and cite
  `file:line`, edge type and confidence from their output.
- The graph artifact `.ast-graph/` is generated; never commit it.
<!-- END managed by install.sh -->
