# {{name}}

{{description}}

## Usage

```hcl
module "{{name_underscore}}" {
  source     = "{{module_source}}"
  project_id = var.project_id
  region     = var.region
  name       = "example"
  labels     = local.common_labels
}
```

See [examples/basic](examples/basic) for a runnable example.

## Conventions (Google Terraform best practices)

- Standard structure: `main.tf`, `variables.tf`, `outputs.tf`, `versions.tf`, `README.md`, `examples/`.
- Every variable has a description and a type; environment-specific inputs have no default.
- Every output has a description and references a resource attribute.
- Stateful resources set `lifecycle { prevent_destroy = true }`.
- Run `terraform fmt`, `terraform validate` and the `terraform-review` skill before opening a PR.

<!-- BEGIN_TF_DOCS -->
<!-- terraform-docs markdown table . (inputs/outputs tables are generated here) -->
<!-- END_TF_DOCS -->
