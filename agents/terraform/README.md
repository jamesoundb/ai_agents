# Terraform Agent (Google Cloud)

**Purpose.** A Terraform engineer for the organization's GCP estate that produces review-ready
code, explains plans as risk, and never applies anything. Built on deterministic skills so two
runs on the same input give the same verdict.

## What it is for

| Ask it | It returns |
|---|---|
| "Review this Terraform PR" | Gate result, findings with rule ids and `file:line`, tool results, prioritized fixes |
| "What will this plan do? Is it safe?" | Change counts, risk-ranked table (data loss, exposure, IAM), per-module changes, drift, verdict |
| "Create a module for X" / "Bootstrap the prod environment" | Compliant scaffold (Google structure, pinned versions, GCS backend, labels), filled in and validated |
| "What breaks if I rename this variable / change this module output?" | Blast radius from the code graph (callers across `.tf` files and nested modules) |
| "Refactor these roots to use the shared module" | Edits with verification (`fmt`, `validate`, review) and a plan-review checklist |

Not for: applying or destroying infrastructure, editing state, storing secrets, Kubernetes
manifests or Helm releases (those belong to the kubernetes and helm agents).

## Definition and skills

Canonical: [`AGENT.md`](AGENT.md). Installed by `install.sh` like every agent.

| skill | role |
|---|---|
| [`terraform-review`](../../skills/terraform-review/SKILL.md) | tree-sitter rule engine: Google style/structure, GCP security, company policy (`tfreview.json`); optional terraform/tflint/trivy |
| [`terraform-plan-review`](../../skills/terraform-plan-review/SKILL.md) | risk-ranked review of `terraform show -json` output; CI gate |
| [`terraform-module-scaffold`](../../skills/terraform-module-scaffold/SKILL.md) | templates for modules and environment roots |
| `code-graph`, `blast-radius`, `code-skeleton` | HCL relationship graph: `var.`, `local.`, `module.x.output`, nested modules from `.terraform/modules/modules.json` |

Requirements: Python 3.9+ (`terraform-review` uses the same tree-sitter venv as `code-graph`);
Terraform CLI optional (enables fmt/validate); `tflint`/`trivy` optional.

## Company tailoring points

- `tfreview.json` per repository: required labels, allowed regions, backend, role exceptions,
  disabled rules, severity overrides.
- `skills/terraform-module-scaffold/templates/`: the organization's canonical module and root
  layout, provider version, label set.
- `STATEFUL` in `planreview.py` and `stateful_types` in `tfreview.json`: resource types whose
  destruction means data loss.
- CI: `terraform-review` and `terraform-plan-review` exit 1 at `--fail-on high`, so they can gate
  any pipeline; no CI system is assumed.
- State: GCS backend only (no Terraform Enterprise/Cloud); rule TF001 enforces it.

## Measured behaviour (fixtures, 2026-09-07)

- Review fixture (root + 3 modules with planted issues): 7 critical, 7 high findings, all
  planted issues caught; clean module reports only low; `terraform fmt`/`validate` integration
  passes on valid modules.
- Plan review: real `terraform show -json` plan (google provider, no credentials) and a synthetic
  plan with destroy/replace/public-IAM/drift produce the expected risk table; streaming `plan -json`
  supported with reduced detail.
- Scaffolded module and root pass `terraform validate` and the review at `--fail-on high`.
