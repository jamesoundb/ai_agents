variable "project_id" {
  description = "Project id for the {{env}} environment."
  type        = string
}

variable "region" {
  description = "Default region."
  type        = string
}

variable "owner" {
  description = "Team that owns these resources (label value)."
  type        = string
}
