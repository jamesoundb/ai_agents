variable "project_id" {
  description = "Google Cloud project that owns the build cluster."
  type        = string
}

variable "region" {
  description = "Region for regional resources."
  type        = string
  default     = "europe-west1"
}

# Deliberately untyped and undescribed: the style rules should catch this.
variable "artifact_bucket_name" {
}
