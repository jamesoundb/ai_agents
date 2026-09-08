# terraform-review rule catalogue

Severities: critical > high > medium > low > info. Override per repo with `severity_overrides`;
switch off with `disabled_rules`. Default gate: `--fail-on high`.

## Structure and style (Google Terraform best practices)

| id | sev | rule |
|---|---|---|
| TF000 | high | HCL parse error, or single-line block with several attributes (invalid HCL) |
| TF001 | high | root module without remote backend, or backend not the company standard (`gcs`) |
| TF002 | low | gcs backend without `prefix` |
| TF003 | medium | `terraform.required_version` missing |
| TF004 | medium | provider without version constraint / configured provider not declared |
| TF005 | low | root module provider not pinned to a minor version (`~> X.Y`) |
| TF006 | low | child module without README.md |
| TF007 | medium | module source unpinned (registry without `version`, git without `?ref=`) |
| TF010 | medium | variable or output without description |
| TF011 | medium | variable without type |
| TF012 | low | variable declared outside variables.tf |
| TF013 | low | output declared outside outputs.tf |
| TF014 | low | environment-specific variable (project, region, zone) with a default |
| TF015 | low | negatively named boolean variable (`disable_*`, `no_*`) |
| TF016 | low | resource name repeats its type |
| TF017 | low | hyphen/uppercase in configuration object names |
| TF018 | low | region/zone variable without validation when `allowed_regions` is configured |
| TF019 | low | nested ternaries on one line |
| TF020 | medium | stateful resource without `lifecycle { prevent_destroy = true }` |
| TF030 | low | file not `terraform fmt` formatted (needs terraform) |
| TF031 | high/low | `terraform validate` error/warning (needs terraform) |

## GCP security

| id | sev | rule |
|---|---|---|
| SEC001 | critical | IAM grant to `allUsers` / `allAuthenticatedUsers` (incl. BigQuery dataset access) |
| SEC002 | high | primitive roles (`roles/owner`, `roles/editor`; viewer = medium) or wildcard permissions in custom roles |
| SEC003 | high/medium | authoritative IAM (`*_iam_policy` high; project/folder/org `*_iam_binding` medium) |
| SEC004 | critical | `google_service_account_key` resource (key in state) |
| SEC005 | critical/high | ingress firewall from 0.0.0.0/0 on sensitive or all ports (critical) / other ports (high) |
| SEC006 | high | bucket without `uniform_bucket_level_access = true` |
| SEC007 | medium | bucket without `public_access_prevention = "enforced"` |
| SEC008 | info | bucket without versioning |
| SEC009 | medium | bucket `force_destroy = true` |
| SEC010 | high | Cloud SQL public IPv4 |
| SEC011 | medium | Cloud SQL public IP without `ssl_mode` / `require_ssl` |
| SEC012 | medium | Cloud SQL `deletion_protection = false` |
| SEC013 | medium | Cloud SQL without automated backups |
| SEC014 | high | GKE without private nodes |
| SEC015 | medium | GKE without workload identity |
| SEC016 | high/medium | GKE legacy ABAC (high) / client certificate issued (medium) |
| SEC017 | low | GKE not on a release channel |
| SEC018 | medium | GKE control plane reachable from any IP |
| SEC019 | medium/high | node pool / instance using the default compute service account (high with cloud-platform scope) |
| SEC020 | high | sensitive-looking output not marked `sensitive` |
| SEC021 | critical | literal secret in .tf/.tfvars (password/secret/token attributes, API keys, private keys) |
| SEC022 | medium | compute instance with public IP (`access_config`) |
| SEC023 | info | instance without shielded VM config |
| SEC024 | medium | VPC with `auto_create_subnetworks = true` |
| SEC025 | low/info | subnet without private Google access (low) / flow logs (info) |
| SEC026 | low | KMS key without `rotation_period` |

## Company conventions (from `tfreview.json`)

| id | sev | rule |
|---|---|---|
| CO001 | medium | labelable resource missing `required_labels` |
| CO002 | medium | region/location/zone literal outside `allowed_regions` |
