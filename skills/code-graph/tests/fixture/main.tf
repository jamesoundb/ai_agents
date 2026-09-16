terraform {
  required_version = ">= 1.5"
}

module "infra" {
  source = "./infra"
}

module "vpc_from_registry" {
  source  = "acme/infra/aws//infra/modules/vpc"    # registry source whose //subdir exists here: a lead
  version = "~> 1.0"
}
