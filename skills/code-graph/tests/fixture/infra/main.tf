terraform {
  required_version = ">= 1.5"
}

module "vpc" {
  source = "./modules/vpc"
  cidr   = var.cidr
}

variable "cidr" {
  type    = string
  default = "10.0.0.0/16"
}

resource "aws_instance" "app" {
  subnet_id = module.vpc.subnet_id
}

output "vpc_id" {
  value = module.vpc.vpc_id
}
