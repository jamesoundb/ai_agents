# {{service}} — {{env}}

Root module (one state per environment, GCS backend `{{state_bucket}}/{{service}}/{{env}}`).

```bash
terraform init
terraform plan -out plan.tfplan
terraform show -json plan.tfplan > plan.json   # review with the terraform-plan-review skill
```

Only the default workspace is used. Check in `.terraform.lock.hcl`.
