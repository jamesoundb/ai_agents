terraform {
  backend "gcs" {
    bucket = "{{state_bucket}}"
    prefix = "{{service}}/{{env}}"
  }
}
