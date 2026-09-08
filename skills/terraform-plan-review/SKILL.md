---
name: terraform-plan-review
description: >
  Turn a Terraform plan into a risk-ranked change review for Google Cloud: destroys and
  replacements of stateful resources (data loss), resources removed from config, IAM grants
  (public members, primitive roles, authoritative policies), internet-open firewalls, public IPs,
  sensitive outputs, drift, and metadata-only noise. Use whenever a plan must be approved, in PR
  or CI pipelines, and before any apply.
allowed-tools: Bash(python3 */terraform-plan-review/scripts/planreview.py *), Bash(*/terraform-plan-review/scripts/planreview.py *), Bash(terraform show -json *), Bash(terraform plan *), Read
---

# terraform-plan-review: what will actually happen, ranked by risk

`scripts/planreview.py` needs only Python 3 and a plan in JSON form. It never talks to Google
Cloud. Prefer the `show -json` form: the streaming form lacks before/after values, so IAM roles
and firewall ports cannot be inspected there.

```bash
terraform plan -out plan.tfplan            # the developer or CI runs this with real credentials
terraform show -json plan.tfplan > plan.json
scripts/planreview.py plan.json [--fail-on high] [--json] [--max-changes 200]
terraform plan -json | scripts/planreview.py -      # streaming form, reduced detail
```

Exit code 1 when a finding reaches `--fail-on` (default `high`), so the same command gates a
CI step or a PR check. `--json` gives machine-readable findings for bots.

## Procedure

1. Obtain the plan JSON. Do not run `terraform plan` yourself unless the developer asked and
   credentials are already configured; never run `apply`, `destroy`, `import`, `state` or
   `taint` commands from this skill.
2. Run the review. Read the **Risk** table first, then **Changes by module**.
3. For every `critical`/`high` row, explain in one or two sentences what the change means for
   the running system (data loss, downtime, exposure, privilege) and what would make it safe:
   `moved`/`removed` blocks instead of delete+create, `create_before_destroy`, a maintenance window,
   narrowing an IAM member, closing a firewall range.
4. Call out `delete_because_no_resource_config` explicitly: the resource vanished from code,
   which is either an intentional decommission or a rename that needs a `moved` block.
5. State the gate result and whether you would approve, with the conditions.

## Risk model (deterministic)

| severity | trigger |
|---|---|
| critical | destroy/replace of a stateful resource (SQL, buckets, BigQuery, Spanner, disks, GKE, KMS, Secret Manager, Pub/Sub ...); project/folder deletion; IAM grant to allUsers/allAuthenticatedUsers; internet-open firewall on sensitive/all ports; service account key creation |
| high | replacement of any resource; resource removed from configuration; primitive roles; authoritative IAM policy change; public IP on instance or Cloud SQL; bucket public-access prevention relaxed; internet-open firewall on other ports; sensitive-looking output not marked sensitive |
| medium | any other destroy; IAM member/binding changes and removals; firewall rule updates; deletion protection turned off; drift detected |
| low / info | more than 100 changes in one plan; label/description-only updates |

The stateful-type list lives at the top of the script (`STATEFUL`); extend it for company
resource types.
