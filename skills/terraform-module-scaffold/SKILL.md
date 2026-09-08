---
name: terraform-module-scaffold
description: >
  Generate a new Terraform module or environment root module for Google Cloud that already follows
  Google's standard structure and the company conventions (versions pinned, GCS backend with
  prefix, labels, described and typed variables, outputs, README with terraform-docs markers,
  runnable example). Use when asked to create, start, bootstrap or scaffold Terraform code.
allowed-tools: Bash(python3 */terraform-module-scaffold/scripts/scaffold.py *), Bash(*/terraform-module-scaffold/scripts/scaffold.py *), Bash(terraform fmt *), Bash(terraform init -backend=false *), Bash(terraform validate *), Read, Write, Edit, Glob
---

# terraform-module-scaffold: start compliant, stay compliant

Templates live in `templates/module` and `templates/root`; `{{placeholders}}` are filled by
`scripts/scaffold.py`. Edit the templates to change company conventions for everyone.

```bash
# reusable module under modules/<name>
scripts/scaffold.py module <name> [--description "..."] [--dir modules/<name>] [--google-provider-version 5.0]

# environment root under environments/<env> (one state per environment, GCS backend)
scripts/scaffold.py root <env> --service <name> --state-bucket <bucket> --project-id <id> \
    [--region us-central1] [--owner <team>] [--dir environments/<env>] [--module-source ../../modules/<name>]

# add --dry-run to preview, --force to overwrite existing files (never the default)
```

## Procedure

1. Ask for what the template cannot guess if the developer did not say: the module's purpose
   (primary resource type), the state bucket and project id for roots, the owning team.
2. Generate with the script. Never hand-write the skeleton; the templates are the standard.
3. Replace the placeholder primary resource in `main.tf` with the real resources, keeping the
   naming rules (singular, underscores, `main` for a single resource of its type) and adding
   `lifecycle { prevent_destroy = true }` on stateful resources.
4. Add inputs to `variables.tf` (description + type, no defaults for environment-specific values)
   and outputs to `outputs.tf` (description, reference resource attributes).
5. Verify: `terraform fmt -recursive`, `terraform init -backend=false && terraform validate`,
   then run the `terraform-review` skill; the scaffold is expected to pass with no findings above
   `low`. Report the files created and the verification results.

## What the templates encode

- Module: `main.tf`, `variables.tf` (project_id, name with validation, region, labels),
  `outputs.tf`, `versions.tf` (`required_version`, provider `~> X.Y`), `README.md` with
  terraform-docs markers, `examples/basic`.
- Root: `backend.tf` (`gcs`, `prefix = <service>/<env>`), `providers.tf`, `versions.tf`,
  `main.tf` with `local.common_labels` (environment, service, owner, managed_by), `variables.tf`,
  `outputs.tf`, `terraform.tfvars`, `README.md` (default workspace only, lock file checked in).
