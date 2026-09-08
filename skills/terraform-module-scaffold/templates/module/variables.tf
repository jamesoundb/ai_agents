variable "project_id" {
  description = "Project id where resources are created (environment-specific, no default)."
  type        = string
}

variable "name" {
  description = "Name of the {{name}} instance; lowercase, hyphen-separated."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,61}[a-z0-9]$", var.name))
    error_message = "name must be lowercase alphanumeric with hyphens, 3-63 characters."
  }
}

variable "region" {
  description = "Region for regional resources (environment-specific, no default)."
  type        = string
}

variable "labels" {
  description = "Labels applied to every labelable resource (environment, owner, cost_center ...)."
  type        = map(string)
  default     = {}
}
