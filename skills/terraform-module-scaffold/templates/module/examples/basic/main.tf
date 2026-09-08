module "{{name_underscore}}" {
  source     = "../../"
  project_id = var.project_id
  region     = var.region
  name       = "{{name}}-example"
  labels = {
    environment = "dev"
    owner       = "platform"
  }
}
