# {{name}} — {{description}}
# Group resources by purpose (network.tf, iam.tf ...) as the module grows; keep this file for the
# primary resources.

locals {
  common_labels = merge(var.labels, {
    managed_by = "terraform"
    module     = "{{name}}"
  })
}

# TODO: replace with the module's primary resource(s). Naming rule: singular, underscores, do not
# repeat the resource type in the name ("main" for the single resource of a type).
resource "google_pubsub_topic" "main" {
  name    = var.name
  project = var.project_id
  labels  = local.common_labels
}
