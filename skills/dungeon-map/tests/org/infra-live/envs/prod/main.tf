module "network" {
  source = "git::http://{HOST}/{ORG}/terraform-google-network.git?ref=v1.0.0"
  name   = "prod"
}

module "dns" {
  source = "{ORG}/dns/google"
}
