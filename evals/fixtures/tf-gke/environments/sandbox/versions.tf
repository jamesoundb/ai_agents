# Provider and backend for the sandbox environment.
terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }

  backend "gcs" {
    bucket = "acme-tfstate-sandbox"
    prefix = "builds/sandbox"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
