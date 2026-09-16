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

variable "ports" {
  type = list(number)
}

resource "aws_security_group" "app" {
  dynamic "node_config" {
    for_each = var.ports
    content {
      from_port = node_config.value       # dynamic-block iterator, not a resource address
    }
  }
  dynamic "log_config" {
    for_each = var.ports
    iterator = log_cfg
    content {
      to_port = log_cfg.value
    }
  }
  tags = {
    vpc =
      module.vpc.vpc_id                   # reference on a later line than the attribute
  }
}

moved {
  from = aws_instance.old
  to   = aws_instance.app
}
