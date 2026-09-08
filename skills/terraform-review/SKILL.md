---
name: terraform-review
description: >
  Deterministic review of Terraform for Google Cloud: Google's style/structure rules (variables,
  outputs, versions, backend, module pinning, naming), GCP security rules (public IAM, primitive
  roles, service account keys, open firewalls, public Cloud SQL, GKE hardening, bucket settings,
  secrets in code) and company conventions (required labels, allowed regions). Use for PR review,
  before opening a PR, when asked "is this Terraform safe/compliant", and as a CI gate.
allowed-tools: Bash(*/terraform-review/scripts/run.sh *), Bash(terraform fmt *), Bash(terraform validate *), Bash(terraform init -backend=false *), Read, Glob, Grep
---

# terraform-review: rules first, opinions second

Runs `scripts/tfreview.py` (tree-sitter HCL parser, no credentials, no network) and, when
installed, `terraform fmt -check`, `terraform init -backend=false` + `terraform validate`,
`tflint` and `trivy config`. Every finding has a rule id, severity, `file:line` and a fix hint, so
two runs on the same code produce the same report. Rule catalogue: `reference/rules.md`;
GCP conventions the rules encode: `reference/gcp-conventions.md`.

```bash
scripts/run.sh [PATH ...] [--config tfreview.json] [--fail-on high|medium|...] [--no-tools] [--json]
scripts/run.sh --rules          # list rule ids and default severities
```

`PATH` defaults to the current directory; every directory containing `.tf` files under it is
reviewed as its own module. Exit code is 1 when a finding reaches `--fail-on` (default `high`)
or a tool fails, which makes it usable as a CI gate in any pipeline.

## Procedure

1. Run the review on the changed directories (or the whole repo for a first pass). Use
   `--no-tools` for a fast pre-check; run with tools before declaring a PR ready.
2. Read the findings top-down: `critical` and `high` first. For each, open only the reported
   `file:line` range and confirm the finding is real in context (rules are syntactic; a value may
   come from a variable or a policy exception).
3. Fix or explain. When a rule is intentionally violated, prefer a change to `tfreview.json`
   (per-repo policy) over silencing the finding in code, and say so in the PR.
4. For a change to a variable, output or module, also run the `blast-radius` skill on the
   Terraform address (`var.x`, `module.y`, `output.z`) to list callers before editing.
5. Report in this shape: gate result, findings table (severity, rule, location, fix), tool
   results, and what you changed or recommend. Quote rule ids so reviewers can look them up.

## Company policy file (`tfreview.json` in the reviewed root)

```json
{
  "required_backend": "gcs",
  "required_labels": ["environment", "owner", "cost_center"],
  "allowed_regions": ["us-central1", "us-east1"],
  "allowed_primitive_roles": [],
  "disabled_rules": ["SEC008"],
  "severity_overrides": {"TF012": "info"},
  "external_tools": true
}
```
Everything is optional; defaults are in `DEFAULT_CONFIG` in the script. Tailor this file per
repository and commit it; the review reports which config file it used.

## Notes

- The parser is lenient. Rule `TF000` flags single-line blocks with several attributes, which
  Terraform rejects even though tree-sitter accepts them; `terraform validate` is the authority.
- `gcloud terraform vet` (policy library) is Google's recommended pre-apply policy check; use it in
  CI for organization constraints this rule set does not encode.
- Findings are about configuration, not live state. Use `terraform-plan-review` for what a plan
  will actually do.
