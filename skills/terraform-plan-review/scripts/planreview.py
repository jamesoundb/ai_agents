#!/usr/bin/env python3
"""
planreview.py — turn a Terraform plan into a risk-ranked, human-readable change review (GCP).

Input: JSON from `terraform show -json plan.tfplan` (preferred) or the NDJSON stream from
`terraform plan -json`. No credentials or providers are needed to run this script.

  planreview.py plan.json [--fail-on high] [--json] [--max-changes 200]
  terraform show -json plan.tfplan | planreview.py -

Risk model (deterministic, severity per finding):
  critical  destroy/replace of a stateful resource (data loss), public IAM grant, internet-open
            firewall on sensitive ports, project/folder deletion
  high      replacement of any resource (downtime), primitive roles, authoritative IAM policy
            changes, public IP exposure, resources removed from config
  medium    any destroy, IAM member/binding changes, firewall changes, deletion-protection off
  low       label/description-only updates, large change sets
"""
import argparse
import json
import re
import sys
from collections import defaultdict

SEVERITIES = ["info", "low", "medium", "high", "critical"]
STATEFUL = {"google_sql_database_instance", "google_storage_bucket", "google_bigquery_dataset", "google_bigquery_table",
            "google_spanner_instance", "google_spanner_database", "google_filestore_instance", "google_redis_instance",
            "google_compute_disk", "google_bigtable_instance", "google_alloydb_cluster", "google_alloydb_instance",
            "google_container_cluster", "google_kms_key_ring", "google_kms_crypto_key", "google_secret_manager_secret",
            "google_pubsub_topic", "google_pubsub_subscription", "google_firestore_database"}
IAM_RE = re.compile(r"^google_.*_iam_(member|binding|policy|audit_config)$")
PRIMITIVE = {"roles/owner", "roles/editor", "roles/viewer"}
SENSITIVE_PORTS = {"22", "3389", "3306", "5432", "1433", "27017", "6379"}
NOISE_KEYS = {"labels", "description", "effective_labels", "terraform_labels", "timeouts"}


def load(path):
    raw = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    raw = raw.strip()
    if raw.startswith("{") and '"format_version"' in raw[:200]:
        return json.loads(raw), "show"
    # NDJSON stream from `terraform plan -json`
    changes, summary, drift, outputs = [], None, [], {}
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        t = ev.get("type")
        if t == "planned_change":
            ch = ev["change"]
            res = ch["resource"]
            action = ch["action"]
            actions = {"create": ["create"], "update": ["update"], "delete": ["delete"], "replace": ["delete", "create"],
                       "read": ["read"], "import": ["create"]}.get(action, [action])
            changes.append({"address": res["addr"], "module_address": res.get("module", ""), "type": res.get("resource_type"),
                            "name": res.get("resource_name"), "change": {"actions": actions, "before": None, "after": None},
                            "action_reason": ch.get("reason")})
        elif t == "resource_drift":
            drift.append({"address": ev["change"]["resource"]["addr"], "change": {"actions": [ev["change"]["action"]]}})
        elif t == "change_summary":
            summary = ev.get("changes")
    return {"resource_changes": changes, "resource_drift": drift, "output_changes": outputs, "_summary": summary,
            "format_version": "stream"}, "stream"


def action_of(actions):
    a = actions or []
    if a == ["no-op"] or not a:
        return "no-op"
    if "delete" in a and "create" in a:
        return "replace"
    if a == ["read"]:
        return "read"
    return a[0]


def diff_keys(before, after, before_sens=None, after_sens=None):
    """Top-level attributes that change between before and after (unknown values excluded)."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return []
    out = []
    for k in sorted(set(before) | set(after)):
        if before.get(k) != after.get(k):
            sens = (isinstance(before_sens, dict) and before_sens.get(k)) or (isinstance(after_sens, dict) and after_sens.get(k))
            out.append(f"{k} (sensitive)" if sens else k)
    return out


def analyze(plan):
    findings = []
    changes = []
    counts = defaultdict(int)

    def add(sev, address, action, why, detail=""):
        findings.append({"severity": sev, "address": address, "action": action, "why": why, "detail": detail})

    for rc in plan.get("resource_changes", []):
        ch = rc.get("change", {})
        act = action_of(ch.get("actions"))
        if act == "no-op":
            continue
        counts[act] += 1
        rtype = rc.get("type") or ""
        addr = rc.get("address")
        before, after = ch.get("before") or {}, ch.get("after") or {}
        keys = diff_keys(before, after, ch.get("before_sensitive"), ch.get("after_sensitive")) if act == "update" else []
        replace_paths = ["/".join(str(p) for p in path) for path in (ch.get("replace_paths") or [])]
        reason = rc.get("action_reason") or ""
        changes.append({"address": addr, "module": rc.get("module_address") or "(root)", "type": rtype, "action": act,
                        "changed": keys, "replace_paths": replace_paths, "reason": reason})

        # --- destruction / replacement ------------------------------------------------------
        if act in ("delete", "replace"):
            if rtype in ("google_project", "google_folder"):
                add("critical", addr, act, "project/folder deletion", "all contained resources are deleted")
            elif rtype in STATEFUL:
                add("critical", addr, act, "data loss: stateful resource is destroyed" + (" and recreated" if act == "replace" else ""),
                    ("forced by: " + ", ".join(replace_paths)) if replace_paths else reason)
            elif act == "replace":
                add("high", addr, act, "resource is replaced (downtime / new identity)", ("forced by: " + ", ".join(replace_paths)) if replace_paths else reason)
            else:
                add("medium", addr, act, "resource is destroyed", reason)
            if reason == "delete_because_no_resource_config":
                add("high", addr, act, "resource removed from configuration", "confirm the removal is intentional; use `moved`/`removed` blocks for renames")
        if act == "update" and rtype in STATEFUL and after.get("deletion_protection") is False and before.get("deletion_protection") is not False:
            add("medium", addr, act, "deletion protection is being turned off", "")

        # --- IAM ----------------------------------------------------------------------------
        if IAM_RE.match(rtype) and act != "delete":
            members = []
            for k in ("member", "members"):
                v = after.get(k)
                if isinstance(v, str):
                    members.append(v)
                elif isinstance(v, list):
                    members += [m for m in v if isinstance(m, str)]
            role = after.get("role") or ""
            pub = [m for m in members if m in ("allUsers", "allAuthenticatedUsers")]
            if pub:
                add("critical", addr, act, f"public IAM grant: {', '.join(pub)}", f"role {role}")
            elif role in PRIMITIVE:
                add("high", addr, act, f"primitive role {role}", ", ".join(members)[:200])
            elif rtype.endswith("_iam_policy"):
                add("high", addr, act, "authoritative IAM policy change (replaces the whole policy)", "")
            else:
                add("medium", addr, act, f"IAM change: {role or 'role unknown'}", ", ".join(members)[:200])
        elif IAM_RE.match(rtype) and act == "delete":
            add("medium", addr, act, "IAM grant removed", f"{before.get('role', '')} {before.get('member', '') or before.get('members', '')}"[:200])

        # --- network exposure -----------------------------------------------------------------
        if rtype == "google_compute_firewall" and act in ("create", "update", "replace"):
            ranges = after.get("source_ranges") or []
            if any(r in ("0.0.0.0/0", "::/0") for r in ranges) and (after.get("direction") or "INGRESS") == "INGRESS":
                ports = [p for a in (after.get("allow") or []) for p in (a.get("ports") or [])]
                protos = [a.get("protocol") for a in (after.get("allow") or [])]
                if not ports or any(p in SENSITIVE_PORTS for p in ports) or "all" in protos:
                    add("critical", addr, act, "firewall open to the internet on sensitive/all ports", f"ports {ports or 'all'}")
                else:
                    add("high", addr, act, "firewall open to the internet", f"ports {ports}")
            elif act != "create":
                add("medium", addr, act, "firewall rule change", ", ".join(keys))
        if rtype == "google_compute_instance" and act != "delete":
            for ni in after.get("network_interface") or []:
                if ni.get("access_config"):
                    add("high", addr, act, "instance gets a public IP", "")
        if rtype == "google_sql_database_instance" and act != "delete":
            for st in after.get("settings") or []:
                for ipc in st.get("ip_configuration") or []:
                    if ipc.get("ipv4_enabled"):
                        add("high", addr, act, "Cloud SQL instance has a public IP", "")
        if rtype == "google_storage_bucket" and act == "update" and before.get("public_access_prevention") == "enforced" and after.get("public_access_prevention") != "enforced":
            add("high", addr, act, "public access prevention is being relaxed", "")
        if rtype == "google_service_account_key" and act == "create":
            add("critical", addr, act, "service account key created (private key stored in state)", "")

        # --- noise classification ---------------------------------------------------------------
        if act == "update" and keys and set(k.split(" ")[0] for k in keys) <= NOISE_KEYS:
            add("info", addr, act, "metadata-only update", ", ".join(keys))

    # --- outputs -------------------------------------------------------------------------------
    outputs = []
    for name, oc in (plan.get("output_changes") or {}).items():
        act = action_of(oc.get("actions"))
        if act == "no-op":
            continue
        sens = bool(oc.get("after_sensitive")) or bool(oc.get("before_sensitive"))
        outputs.append({"name": name, "action": act, "sensitive": sens})
        if re.search(r"(password|secret|token|private_key|key)$", name, re.I) and not sens:
            add("high", f"output.{name}", act, "output looks sensitive but is not marked sensitive", "")

    # --- drift --------------------------------------------------------------------------------
    drift = [{"address": d.get("address"), "action": action_of(d.get("change", {}).get("actions"))} for d in plan.get("resource_drift") or []]
    if drift:
        add("medium", "(state)", "drift", f"{len(drift)} resource(s) drifted outside Terraform", ", ".join(d["address"] for d in drift[:10]))

    total = sum(counts.values())
    if total > 100:
        add("low", "(plan)", "size", f"{total} resource changes in one plan", "Google guidance: keep a state under ~100 resources; consider splitting the root module")
    prio = {s: i for i, s in enumerate(SEVERITIES)}
    findings.sort(key=lambda f: (-prio[f["severity"]], f["address"]))
    return {"counts": dict(counts), "findings": findings, "changes": changes, "outputs": outputs, "drift": drift,
            "terraform_version": plan.get("terraform_version"), "format": plan.get("format_version")}


def render(res, fail_on, max_changes):
    c = res["counts"]
    lines = ["## Terraform plan review", "",
             f"Changes: create {c.get('create', 0)}, update {c.get('update', 0)}, replace {c.get('replace', 0)}, "
             f"delete {c.get('delete', 0)}, read {c.get('read', 0)}" + (f"  (terraform {res['terraform_version']})" if res.get("terraform_version") else "")]
    if res["findings"]:
        lines += ["", "### Risk", "", "| severity | resource | action | why | detail |", "|---|---|---|---|---|"]
        for f in res["findings"]:
            lines.append(f"| {f['severity']} | `{f['address']}` | {f['action']} | {f['why']} | {f['detail']} |")
    else:
        lines += ["", "### Risk", "", "No risky changes detected by the rule set."]
    by_mod = defaultdict(list)
    for ch in res["changes"]:
        by_mod[ch["module"]].append(ch)
    lines += ["", "### Changes by module", ""]
    shown = 0
    for mod in sorted(by_mod):
        lines.append(f"**{mod}**")
        for ch in sorted(by_mod[mod], key=lambda x: (x["action"], x["address"])):
            if shown >= max_changes:
                break
            extra = ""
            if ch["action"] == "update" and ch["changed"]:
                extra = "  changed: " + ", ".join(ch["changed"][:12]) + (" ..." if len(ch["changed"]) > 12 else "")
            elif ch["action"] == "replace" and ch["replace_paths"]:
                extra = "  forced by: " + ", ".join(ch["replace_paths"])
            elif ch["reason"]:
                extra = f"  ({ch['reason']})"
            lines.append(f"- {ch['action']:<8} `{ch['address']}`{extra}")
            shown += 1
        lines.append("")
    if len(res["changes"]) > shown:
        lines.append(f"... {len(res['changes']) - shown} more changes not listed (--max-changes)")
    if res["outputs"]:
        lines += ["### Outputs", ""] + [f"- {o['action']} {o['name']}" + (" (sensitive)" if o["sensitive"] else "") for o in res["outputs"]] + [""]
    if res["drift"]:
        lines += ["### Drift (state vs. real infrastructure)", ""] + [f"- {d['action']} `{d['address']}`" for d in res["drift"]] + [""]
    gate = SEVERITIES.index(fail_on)
    failing = [f for f in res["findings"] if SEVERITIES.index(f["severity"]) >= gate]
    lines.append(f"Gate (--fail-on {fail_on}): " + ("FAIL: " + ", ".join(sorted({f['address'] for f in failing})) if failing else "PASS"))
    return "\n".join(lines), bool(failing)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan", help="plan JSON file, or - for stdin")
    ap.add_argument("--fail-on", default="high", choices=SEVERITIES)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--max-changes", type=int, default=200)
    a = ap.parse_args(argv)
    plan, fmt = load(a.plan)
    res = analyze(plan)
    if a.json:
        gate = SEVERITIES.index(a.fail_on)
        res["fail"] = bool([f for f in res["findings"] if SEVERITIES.index(f["severity"]) >= gate])
        print(json.dumps(res, indent=1))
        return 1 if res["fail"] else 0
    text, failing = render(res, a.fail_on, a.max_changes)
    print(text)
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
