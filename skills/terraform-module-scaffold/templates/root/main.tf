# Root module for {{service}} in the {{env}} environment. Instantiate service modules here; keep
# environment differences in terraform.tfvars, not in code.

locals {
  common_labels = {
    environment = "{{env}}"
    service     = "{{service}}"
    owner       = var.owner
    managed_by  = "terraform"
  }
}

module "{{service_underscore}}" {
  source     = "{{module_source}}"
  project_id = var.project_id
  region     = var.region
  name       = "{{service}}-{{env}}"
  labels     = local.common_labels
}
