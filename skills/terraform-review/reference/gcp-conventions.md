# GCP Terraform conventions encoded by this skill

Derived from Google Cloud's "Best practices for using Terraform" (general style and structure,
root modules, security), retrieved 2026-09-07, plus company policy (tailor `tfreview.json`).

**Structure.** Standard module structure: `main.tf`, `variables.tf`, `outputs.tf`, `versions.tf`,
`README.md`, `examples/`. Group resources by purpose (`network.tf`, `iam.tf`), not one file per
resource. Repository layout: `modules/` for reusable modules, `environments/<env>/` for root
modules, each with `backend.tf` (GCS bucket + prefix) and `terraform.tfvars`. Only the default
workspace. Check in `.terraform.lock.hcl`. Keep a state under ~100 resources.

**Naming.** Underscores between words; singular resource names; `main` for the single resource of
a type; never repeat the type in the name. Units in numeric variable names (`ram_size_gb`);
positive boolean names (`enable_x`).

**Variables and outputs.** Every variable has a description and a type; environment-specific
inputs (`project_id`, `region`) have no default; parameterize only what varies. Every output has
a description and references a resource attribute (implicit dependency). Mark secrets
`sensitive = true`.

**Versions.** `required_version` everywhere; provider constraints everywhere; root modules pin
providers to a minor version; module sources pinned (`version` or `?ref=`).

**Security.** GCS backend with restricted bucket access; no secrets in state where avoidable
(no `google_service_account_key`, no plaintext secrets, use Secret Manager); additive IAM
(`*_iam_member`) over authoritative; predefined/custom roles over primitive; no public members;
private GKE nodes with workload identity; private Cloud SQL; uniform bucket-level access with
public access prevention; deletion protection on stateful resources; `gcloud terraform vet` for
organization policies before apply.

**Company policy knobs.** `required_labels` (recommended: environment, owner, cost_center),
`allowed_regions` (data residency), `required_backend`, `allowed_primitive_roles` (exceptions).
