#!/usr/bin/env python3
"""
tfreview.py — deterministic Terraform review for Google Cloud, no cloud credentials needed.

Parses .tf/.tfvars files with tree-sitter (HCL grammar), applies style/structure rules from
Google's Terraform best practices and GCP security rules, and optionally runs the external tools
that are installed (terraform fmt/validate, tflint, trivy). Findings are deterministic and carry a
rule id, severity, file:line and a fix hint, so the same input always yields the same report.

Usage:
  tfreview.py [PATH ...] [--config tfreview.json] [--fail-on high] [--no-tools] [--json] [--rules]

Config (tfreview.json next to the reviewed root, or --config): see reference/rules.md.
Dependencies: pip install tree-sitter tree-sitter-language-pack
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict

try:
    from tree_sitter_language_pack import get_parser
except ImportError:
    sys.stderr.write("tfreview: missing dependencies. Run: pip install tree-sitter tree-sitter-language-pack\n")
    sys.exit(2)

SEVERITIES = ["info", "low", "medium", "high", "critical"]
DEFAULT_CONFIG = {
    "required_backend": "gcs",
    "required_labels": [],          # e.g. ["environment", "owner", "cost_center"]
    "allowed_regions": [],          # e.g. ["us-central1", "europe-west1"]; empty = no check
    "allowed_primitive_roles": [],  # e.g. ["roles/viewer"] to tolerate viewer
    "stateful_types": ["google_sql_database_instance", "google_storage_bucket", "google_bigquery_dataset",
                       "google_spanner_instance", "google_filestore_instance", "google_redis_instance",
                       "google_compute_disk", "google_bigtable_instance", "google_alloydb_cluster"],
    "labelable_types": ["google_compute_instance", "google_storage_bucket", "google_sql_database_instance",
                        "google_container_cluster", "google_bigquery_dataset", "google_pubsub_topic",
                        "google_cloud_run_v2_service", "google_compute_disk", "google_project"],
    "sensitive_ports": ["22", "3389", "3306", "5432", "1433", "27017", "6379"],
    "disabled_rules": [],
    "severity_overrides": {},       # {"TF012": "info"}
    "external_tools": True,
}
EXCLUDE_DIRS = {".git", ".terraform", "node_modules", ".ast-graph", "__pycache__"}
PRIMITIVE_ROLES = {"roles/owner", "roles/editor", "roles/viewer"}
SECRET_PATTERNS = [
    (re.compile(r"AIza[0-9A-Za-z_\-]{35}"), "Google API key"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), "private key material"),
    (re.compile(r"\"private_key_id\"\s*:"), "service account JSON key"),
    (re.compile(r"ya29\.[0-9A-Za-z\-_]+"), "OAuth access token"),
]
ONE_LINE_BLOCK_RE = re.compile(r'^\s*[a-z_]+(?:\s+"[^"]*")*\s*\{[^{}]*=[^{}]*\s[a-z_]+\s*=[^{}]*\}\s*$')
SECRET_ATTR_RE = re.compile(r"(password|secret|token|api_key|private_key|client_secret)$", re.I)


# ----------------------------------------------------------------------------------------------
# HCL model
# ----------------------------------------------------------------------------------------------
class Block:
    __slots__ = ("type", "labels", "attrs", "blocks", "line", "end_line", "file")

    def __init__(self, type_, labels, line, end_line, file):
        self.type, self.labels, self.line, self.end_line, self.file = type_, labels, line, end_line, file
        self.attrs = {}   # key -> (expression text, line)
        self.blocks = []

    def attr(self, key, default=None):
        return self.attrs[key][0] if key in self.attrs else default

    def attr_line(self, key):
        return self.attrs[key][1] if key in self.attrs else self.line

    def find(self, type_):
        return [b for b in self.blocks if b.type == type_]

    def first(self, type_):
        f = self.find(type_)
        return f[0] if f else None

    @property
    def address(self):
        if self.type in ("resource", "data") and len(self.labels) >= 2:
            return ("data." if self.type == "data" else "") + f"{self.labels[0]}.{self.labels[1]}"
        return " ".join([self.type] + self.labels)


_parser = None


def parse_hcl(path, src):
    global _parser
    if _parser is None:
        _parser = get_parser("hcl")
    tree = _parser.parse(src)

    def text(n):
        return src[n.start_byte:n.end_byte].decode("utf-8", "replace")

    def strip(s):
        s = s.strip()
        return s[1:-1] if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'" else s

    def body_into(block, body):
        for c in body.named_children:
            if c.type == "attribute" and c.named_children:
                key = text(c.named_children[0])
                expr = text(c.named_children[1]) if len(c.named_children) > 1 else ""
                block.attrs[key] = (expr, c.start_point[0] + 1)
            elif c.type == "block":
                block.blocks.append(make_block(c))

    def make_block(n):
        btype, labels, body = None, [], None
        for c in n.named_children:
            if c.type == "identifier" and btype is None:
                btype = text(c)
            elif c.type == "string_lit":
                labels.append(strip(text(c)))
            elif c.type == "body":
                body = c
        b = Block(btype or "?", labels, n.start_point[0] + 1, n.end_point[0] + 1, path)
        if body is not None:
            body_into(b, body)
        return b

    root = Block("file", [], 1, src.count(b"\n") + 1, path)
    for c in tree.root_node.named_children:
        if c.type == "body":
            body_into(root, c)
    return root, tree.root_node.has_error


# ----------------------------------------------------------------------------------------------
# Findings
# ----------------------------------------------------------------------------------------------
class Report:
    def __init__(self, config):
        self.config = config
        self.findings = []
        self.tools = []

    def add(self, rule, severity, file, line, message, fix=None, address=None):
        if rule in self.config["disabled_rules"]:
            return
        severity = self.config["severity_overrides"].get(rule, severity)
        self.findings.append({"rule": rule, "severity": severity, "file": file, "line": line,
                              "message": message, "fix": fix, "address": address})


def val_is_true(expr):
    return expr is not None and expr.strip() == "true"


def val_is_false(expr):
    return expr is not None and expr.strip() == "false"


def literal_string(expr):
    """Return the literal string if expr is a plain quoted string with no interpolation, else None."""
    if expr is None:
        return None
    e = expr.strip()
    if len(e) >= 2 and e[0] == '"' and e[-1] == '"' and "${" not in e:
        return e[1:-1]
    return None


def list_literals(expr):
    return re.findall(r"\"([^\"]*)\"", expr or "")


# ----------------------------------------------------------------------------------------------
# Rules — structure and style (Google Terraform best practices)
# ----------------------------------------------------------------------------------------------
def check_directory(dir_path, abs_dir, files, rep):
    """Directory-level rules. `files` maps filename -> (root Block, has_error)."""
    cfg = rep.config
    all_blocks = [(fn, b) for fn, (root, _) in files.items() for b in root.blocks]
    tf_blocks = [b for fn, b in all_blocks if b.type == "terraform"]
    providers = [b for fn, b in all_blocks if b.type == "provider"]
    backends = [bb for b in tf_blocks for bb in b.find("backend")]
    is_root = bool(providers or backends or any(fn.endswith(".tfvars") for fn in files) or "backend.tf" in files)
    rel = lambda fn: fn if dir_path == "." else os.path.join(dir_path, fn)

    if is_root:
        if not backends:
            rep.add("TF001", "high", rel("main.tf" if "main.tf" in files else next(iter(files))), 1,
                    "root module has no remote backend; state would be local",
                    f'add terraform {{ backend "{cfg["required_backend"]}" {{ bucket = ..., prefix = ... }} }} in backend.tf')
        else:
            for be in backends:
                if be.labels and be.labels[0] != cfg["required_backend"]:
                    rep.add("TF001", "high", be.file, be.line, f'backend is "{be.labels[0]}", company standard is "{cfg["required_backend"]}"',
                            f'use backend "{cfg["required_backend"]}" with a per-environment prefix')
                elif be.labels and be.labels[0] == "gcs" and "prefix" not in be.attrs:
                    rep.add("TF002", "low", be.file, be.line, "gcs backend without prefix; one bucket per state is wasteful and mixes environments",
                            'set prefix = "<service>/<environment>"')
    if not any("required_version" in b.attrs for b in tf_blocks):
        rep.add("TF003", "medium", rel("versions.tf" if "versions.tf" in files else next(iter(files))), 1,
                "terraform.required_version is not set", 'terraform { required_version = ">= 1.5, < 2.0" }')
    rp_blocks = [bb for b in tf_blocks for bb in b.find("required_providers")]
    declared = {}
    for rp in rp_blocks:
        for k, (expr, line) in rp.attrs.items():
            declared[k] = (expr, line, rp.file)
            if "version" not in expr:
                rep.add("TF004", "medium", rp.file, line, f'provider "{k}" has no version constraint',
                        f'{k} = {{ source = "hashicorp/{k}", version = "~> 5.0" }}')
            elif is_root and not re.search(r"version\s*=\s*\"(~>|=)\s*\d+\.\d+", expr):
                rep.add("TF005", "low", rp.file, line, f'root module provider "{k}" is not pinned to a minor version',
                        'Google guidance for root modules: pin to a minor version, e.g. version = "~> 5.10"')
    used = {p.labels[0] for p in providers if p.labels}
    for p in used:
        if p not in declared and not is_root:
            pass
    for p in providers:
        if p.labels and p.labels[0] not in declared:
            rep.add("TF004", "medium", p.file, p.line, f'provider "{p.labels[0]}" is configured but not declared in required_providers', "declare it in versions.tf")
    if any(fn.endswith(".tf") for fn in files) and "README.md" not in os.listdir(abs_dir) and not is_root:
        rep.add("TF006", "low", rel("main.tf" if "main.tf" in files else next(iter(files))), 1,
                "module has no README.md", "add README.md (terraform-docs can generate inputs/outputs tables)")
    # variables/outputs placement
    for fn, b in all_blocks:
        if b.type == "variable" and fn != "variables.tf":
            rep.add("TF012", "low", b.file, b.line, f'variable "{b.labels[0] if b.labels else "?"}" declared in {fn}', "declare all variables in variables.tf")
        if b.type == "output" and fn != "outputs.tf":
            rep.add("TF013", "low", b.file, b.line, f'output "{b.labels[0] if b.labels else "?"}" declared in {fn}', "declare all outputs in outputs.tf")


def check_block(b, rep, ctx):
    cfg = rep.config
    f, ln = b.file, b.line
    name = b.labels[-1] if b.labels else ""
    # ---- naming ------------------------------------------------------------------------
    if b.type in ("resource", "data", "module", "variable", "output") and name:
        if re.search(r"[A-Z\-]", name):
            rep.add("TF017", "low", f, ln, f'{b.type} "{name}" uses hyphens or uppercase', "use lowercase words delimited by underscores")
        if b.type == "resource" and len(b.labels) == 2:
            rtype_words = set(b.labels[0].split("_")[1:])  # drop provider prefix
            if name in rtype_words or (len(rtype_words) > 1 and name == "_".join(b.labels[0].split("_")[1:])):
                rep.add("TF016", "low", f, ln, f'resource name "{name}" repeats its resource type', 'name it by role ("main", "primary") not by type')
    # ---- variables ---------------------------------------------------------------------
    if b.type == "variable":
        if "description" not in b.attrs or literal_string(b.attr("description")) == "":
            rep.add("TF010", "medium", f, ln, f'variable "{name}" has no description', "variables must have descriptions")
        if "type" not in b.attrs:
            rep.add("TF011", "medium", f, ln, f'variable "{name}" has no type', "give variables defined types")
        if re.match(r"(disable|no|not|skip|dont)_", name):
            rep.add("TF015", "low", f, ln, f'boolean-looking variable "{name}" is negatively named', 'use positive names, e.g. enable_external_access')
        if name in ("project", "project_id", "region", "zone") and "default" in b.attrs:
            rep.add("TF014", "low", f, ln, f'environment-specific variable "{name}" has a default', "omit defaults for environment-specific values")
        if cfg["allowed_regions"] and name in ("region", "zone") and "validation" not in {bb.type for bb in b.blocks}:
            rep.add("TF018", "low", f, ln, f'variable "{name}" has no validation against allowed regions', f'add validation {{ condition = contains({json.dumps(cfg["allowed_regions"])}, var.{name}) }}')
    # ---- outputs -----------------------------------------------------------------------
    if b.type == "output":
        if "description" not in b.attrs:
            rep.add("TF010", "medium", f, ln, f'output "{name}" has no description', "provide meaningful descriptions for all outputs")
        val = b.attr("value", "")
        if SECRET_ATTR_RE.search(val.replace(".", "_")) or re.search(r"(private_key|password|secret|token)", val) :
            if not val_is_true(b.attr("sensitive")):
                rep.add("SEC020", "high", f, ln, f'output "{name}" looks sensitive but is not marked sensitive', "add sensitive = true")
    # ---- modules -----------------------------------------------------------------------
    if b.type == "module":
        src = literal_string(b.attr("source")) or ""
        if src.startswith(("git::", "github.com", "git@")) and "ref=" not in src:
            rep.add("TF007", "medium", f, b.attr_line("source"), f'module "{name}" git source has no ?ref=', "pin to a tag or commit: ...?ref=v1.2.0")
        elif src and not src.startswith((".", "/")) and "://" not in src and "version" not in b.attrs:
            rep.add("TF007", "medium", f, b.attr_line("source"), f'module "{name}" registry source has no version', 'add version = "~> 1.0"')
    # ---- expressions -------------------------------------------------------------------
    for k, (expr, line) in b.attrs.items():
        if expr.count("?") >= 2 and "\n" not in expr:
            rep.add("TF019", "low", f, line, f'attribute "{k}" nests multiple ternaries on one line', "split into locals")
        lit = literal_string(expr)
        if lit is not None and SECRET_ATTR_RE.search(k) and lit and not lit.startswith("$"):
            rep.add("SEC021", "critical", f, line, f'attribute "{k}" has a literal secret value', "read it from Secret Manager (data.google_secret_manager_secret_version) or a sensitive variable")
        for pat, what in SECRET_PATTERNS:
            if pat.search(expr):
                rep.add("SEC021", "critical", f, line, f"{what} embedded in configuration", "remove it and rotate the credential")
    # ---- GCP resources -----------------------------------------------------------------
    if b.type == "resource" and len(b.labels) == 2:
        check_gcp_resource(b, rep, ctx)
    for bb in b.blocks:
        check_block(bb, rep, ctx)


IAM_RE = re.compile(r"^google_.*_iam_(member|binding|policy|audit_config)$")


def members_in(b):
    out = []
    if "member" in b.attrs:
        out.append((b.attr("member"), b.attr_line("member")))
    if "members" in b.attrs:
        out.append((b.attr("members"), b.attr_line("members")))
    return out


def check_gcp_resource(b, rep, ctx):
    cfg = rep.config
    rtype, name = b.labels
    f, ln, addr = b.file, b.line, b.address
    lifecycle = b.first("lifecycle")

    # IAM ---------------------------------------------------------------------------------
    if IAM_RE.match(rtype):
        for expr, line in members_in(b):
            if "allUsers" in expr:
                rep.add("SEC001", "critical", f, line, f"{addr} grants access to allUsers (public)", "remove the public member; use signed URLs / IAP / authenticated identities", addr)
            elif "allAuthenticatedUsers" in expr:
                rep.add("SEC001", "critical", f, line, f"{addr} grants access to allAuthenticatedUsers (any Google account)", "restrict to specific principals or groups", addr)
        role = literal_string(b.attr("role"))
        if role in PRIMITIVE_ROLES and role not in cfg["allowed_primitive_roles"]:
            rep.add("SEC002", "high" if role != "roles/viewer" else "medium", f, b.attr_line("role"), f"{addr} grants primitive role {role}", "use predefined or custom roles with least privilege", addr)
        if rtype.endswith("_iam_policy"):
            rep.add("SEC003", "high", f, ln, f"{addr} is authoritative for the whole IAM policy (google_*_iam_policy)", "prefer google_*_iam_member (additive) unless the policy is intentionally owned here", addr)
        elif rtype.endswith("_iam_binding") and rtype.startswith(("google_project_", "google_folder_", "google_organization_")):
            rep.add("SEC003", "medium", f, ln, f"{addr} is authoritative for a role at project/folder/org level", "prefer *_iam_member unless you intend to remove other members of this role", addr)
    if rtype == "google_service_account_key":
        rep.add("SEC004", "critical", f, ln, f"{addr} creates a service account key; the private key lands in state", "use workload identity / impersonation instead of keys", addr)
    if rtype == "google_project_iam_custom_role":
        perms = b.attr("permissions", "")
        if re.search(r"\"[a-z]+\.\*\"|\"\*\"", perms):
            rep.add("SEC002", "high", f, b.attr_line("permissions"), f"{addr} custom role uses wildcard permissions", "enumerate the exact permissions", addr)

    # Firewall ----------------------------------------------------------------------------
    if rtype == "google_compute_firewall":
        direction = literal_string(b.attr("direction")) or "INGRESS"
        ranges = list_literals(b.attr("source_ranges", ""))
        if direction == "INGRESS" and any(r in ("0.0.0.0/0", "::/0") for r in ranges):
            ports = [p for a in b.find("allow") for p in list_literals(a.attr("ports", ""))]
            protos = [literal_string(a.attr("protocol")) for a in b.find("allow")]
            open_all = (not ports and any(p in ("all", "tcp", "udp") for p in protos)) or "all" in protos
            sensitive = [p for p in ports if any(p == sp or (("-" in p) and int(p.split("-")[0]) <= int(sp) <= int(p.split("-")[1])) for sp in cfg["sensitive_ports"] if p.replace("-", "").isdigit())]
            if open_all or sensitive:
                rep.add("SEC005", "critical", f, b.attr_line("source_ranges"), f"{addr} allows ingress from the internet on {'all ports' if open_all else ', '.join(sensitive)}", "restrict source_ranges (IAP TCP forwarding: 35.235.240.0/20 for SSH/RDP) or use target tags + specific ports", addr)
            else:
                rep.add("SEC005", "high", f, b.attr_line("source_ranges"), f"{addr} allows ingress from 0.0.0.0/0 on {', '.join(ports) or 'unspecified ports'}", "confirm this is a public-facing service; narrow ports and add target_tags", addr)

    # Storage -----------------------------------------------------------------------------
    if rtype == "google_storage_bucket":
        if not val_is_true(b.attr("uniform_bucket_level_access")):
            rep.add("SEC006", "high", f, ln, f"{addr} does not enforce uniform_bucket_level_access", "set uniform_bucket_level_access = true", addr)
        if literal_string(b.attr("public_access_prevention")) != "enforced":
            rep.add("SEC007", "medium", f, ln, f"{addr} does not set public_access_prevention = \"enforced\"", 'add public_access_prevention = "enforced"', addr)
        v = b.first("versioning")
        if v is None or not val_is_true(v.attr("enabled")):
            rep.add("SEC008", "info", f, ln, f"{addr} has no object versioning", "versioning { enabled = true } for buckets holding state or data you cannot recreate", addr)
        if val_is_true(b.attr("force_destroy")):
            rep.add("SEC009", "medium", f, b.attr_line("force_destroy"), f"{addr} has force_destroy = true (bucket and objects deleted on destroy)", "set force_destroy = false outside ephemeral environments", addr)

    # Cloud SQL ---------------------------------------------------------------------------
    if rtype == "google_sql_database_instance":
        settings = b.first("settings")
        ipc = settings.first("ip_configuration") if settings else None
        if ipc is None or not val_is_false(ipc.attr("ipv4_enabled")):
            rep.add("SEC010", "high", f, (ipc.line if ipc else ln), f"{addr} has a public IPv4 address (ipv4_enabled defaults to true)", "set ipv4_enabled = false and private_network = <vpc self link>, or require ssl + authorized_networks", addr)
        if ipc is not None and not val_is_false(ipc.attr("ipv4_enabled")) and "ssl_mode" not in ipc.attrs and not val_is_true(ipc.attr("require_ssl")):
            rep.add("SEC011", "medium", f, ipc.line, f"{addr} public IP without ssl_mode / require_ssl", 'ssl_mode = "ENCRYPTED_ONLY"', addr)
        if val_is_false(b.attr("deletion_protection")):
            rep.add("SEC012", "medium", f, b.attr_line("deletion_protection"), f"{addr} has deletion_protection = false", "keep deletion protection on for non-ephemeral databases", addr)
        bc = settings.first("backup_configuration") if settings else None
        if bc is None or not val_is_true(bc.attr("enabled")):
            rep.add("SEC013", "medium", f, ln, f"{addr} has no automated backups", "settings { backup_configuration { enabled = true, point_in_time_recovery_enabled = true } }", addr)

    # GKE ---------------------------------------------------------------------------------
    if rtype == "google_container_cluster":
        pcc = b.first("private_cluster_config")
        if pcc is None or not val_is_true(pcc.attr("enable_private_nodes")):
            rep.add("SEC014", "high", f, ln, f"{addr} nodes are not private", "private_cluster_config { enable_private_nodes = true, master_ipv4_cidr_block = ... }", addr)
        if b.first("workload_identity_config") is None:
            rep.add("SEC015", "medium", f, ln, f"{addr} has no workload_identity_config", 'workload_identity_config { workload_pool = "<project>.svc.id.goog" }', addr)
        if val_is_true(b.attr("enable_legacy_abac")):
            rep.add("SEC016", "high", f, b.attr_line("enable_legacy_abac"), f"{addr} enables legacy ABAC", "remove enable_legacy_abac", addr)
        ma = b.first("master_auth")
        ccc = ma.first("client_certificate_config") if ma else None
        if ccc is not None and val_is_true(ccc.attr("issue_client_certificate")):
            rep.add("SEC016", "medium", f, ccc.line, f"{addr} issues a client certificate", "issue_client_certificate = false; use IAM/OIDC", addr)
        if b.first("release_channel") is None:
            rep.add("SEC017", "low", f, ln, f"{addr} is not on a release channel", 'release_channel { channel = "REGULAR" }', addr)
        if b.first("master_authorized_networks_config") is None and (pcc is None or not val_is_true(pcc.attr("enable_private_endpoint"))):
            rep.add("SEC018", "medium", f, ln, f"{addr} control plane is reachable from any IP", "add master_authorized_networks_config or enable_private_endpoint = true", addr)
    if rtype == "google_container_node_pool":
        nc = b.first("node_config")
        if nc is not None and "service_account" not in nc.attrs:
            rep.add("SEC019", "medium", f, nc.line, f"{addr} nodes use the default compute service account", "create a dedicated least-privilege node service account", addr)

    # Compute -----------------------------------------------------------------------------
    if rtype == "google_compute_instance":
        for ni in b.find("network_interface"):
            if ni.find("access_config"):
                rep.add("SEC022", "medium", f, ni.first("access_config").line, f"{addr} has a public IP (access_config)", "remove access_config; use Cloud NAT / IAP for egress and access", addr)
        sa = b.first("service_account")
        if sa is not None and "email" not in sa.attrs and "cloud-platform" in sa.attr("scopes", ""):
            rep.add("SEC019", "high", f, sa.line, f"{addr} runs as the default compute service account with cloud-platform scope", "set service_account.email to a dedicated SA", addr)
        if b.first("shielded_instance_config") is None:
            rep.add("SEC023", "info", f, ln, f"{addr} has no shielded_instance_config", "shielded_instance_config { enable_secure_boot = true, enable_vtpm = true, enable_integrity_monitoring = true }", addr)
    if rtype == "google_compute_network" and val_is_true(b.attr("auto_create_subnetworks")):
        rep.add("SEC024", "medium", f, b.attr_line("auto_create_subnetworks"), f"{addr} auto-creates subnets in every region", "auto_create_subnetworks = false and define subnets explicitly", addr)
    if rtype == "google_compute_subnetwork":
        if not val_is_true(b.attr("private_ip_google_access")):
            rep.add("SEC025", "low", f, ln, f"{addr} has private_ip_google_access disabled", "private_ip_google_access = true", addr)
        if b.first("log_config") is None:
            rep.add("SEC025", "info", f, ln, f"{addr} has no VPC flow logs", "log_config { aggregation_interval = \"INTERVAL_5_SEC\", flow_sampling = 0.5, metadata = \"INCLUDE_ALL_METADATA\" }", addr)

    # BigQuery / KMS / Pub/Sub --------------------------------------------------------------
    if rtype == "google_bigquery_dataset":
        for a in b.find("access"):
            if literal_string(a.attr("special_group")) in ("allAuthenticatedUsers",) or "allUsers" in a.attr("iam_member", ""):
                rep.add("SEC001", "critical", f, a.line, f"{addr} grants dataset access to all (authenticated) users", "remove the public access block", addr)
    if rtype == "google_kms_crypto_key" and "rotation_period" not in b.attrs:
        rep.add("SEC026", "low", f, ln, f"{addr} has no rotation_period", 'rotation_period = "7776000s" (90 days)', addr)

    # Stateful resources: deletion protection --------------------------------------------
    if rtype in cfg["stateful_types"]:
        if lifecycle is None or not val_is_true(lifecycle.attr("prevent_destroy")):
            rep.add("TF020", "medium", f, ln, f"stateful resource {addr} has no lifecycle {{ prevent_destroy = true }}", "add prevent_destroy for databases, buckets and disks that hold data", addr)

    # Company conventions -------------------------------------------------------------------
    if cfg["required_labels"] and rtype in cfg["labelable_types"]:
        labels_expr = b.attr("labels", "")
        missing = [l for l in cfg["required_labels"] if not re.search(rf"\b{re.escape(l)}\b", labels_expr)]
        if "labels" not in b.attrs:
            rep.add("CO001", "medium", f, ln, f"{addr} has no labels; required: {', '.join(cfg['required_labels'])}", "labels = local.common_labels (define common labels once in locals)", addr)
        elif missing and not re.search(r"(local\.|var\.|merge\()", labels_expr):
            rep.add("CO001", "medium", f, b.attr_line("labels"), f"{addr} labels missing: {', '.join(missing)}", "add the required labels or merge(local.common_labels, {...})", addr)
    if cfg["allowed_regions"]:
        for k in ("region", "location", "zone"):
            lit = literal_string(b.attr(k))
            if lit and not any(lit.startswith(r) for r in cfg["allowed_regions"]) and lit not in ("US", "EU", "global"):
                rep.add("CO002", "medium", f, b.attr_line(k), f"{addr} uses {k} \"{lit}\" outside allowed regions", f"allowed: {', '.join(cfg['allowed_regions'])}", addr)


# ----------------------------------------------------------------------------------------------
# External tools (optional)
# ----------------------------------------------------------------------------------------------
def run_tool(cmd, cwd, timeout=300):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def external_tools(dirs, rep, root):
    tf = shutil.which("terraform")
    for d in dirs:
        reld = os.path.relpath(d, root) or "."
        if tf:
            rc, out = run_tool([tf, "fmt", "-check", "-diff", "-no-color"], d)
            rep.tools.append({"tool": "terraform fmt -check", "dir": reld, "ok": rc == 0, "output": out[:4000]})
            if rc != 0:
                for m in re.finditer(r"^([^\s].*\.tf)$", out, flags=re.M):
                    rep.add("TF030", "low", os.path.join(reld, m.group(1)) if not m.group(1).startswith(reld) else m.group(1), 1, "file is not terraform fmt formatted", "run terraform fmt")
            rc, out = run_tool([tf, "init", "-backend=false", "-input=false", "-no-color"], d, timeout=600)
            if rc == 0:
                rc, out = run_tool([tf, "validate", "-no-color", "-json"], d)
                try:
                    data = json.loads(out)
                    ok = data.get("valid", False)
                    for diag in data.get("diagnostics", []):
                        r = diag.get("range") or {}
                        fn = r.get("filename", "?")
                        rep.add("TF031", "high" if diag.get("severity") == "error" else "low",
                                os.path.join(reld, fn) if reld != "." else fn, (r.get("start") or {}).get("line", 1),
                                f"terraform validate: {diag.get('summary')}: {diag.get('detail', '')}".strip(), None)
                except ValueError:
                    ok = rc == 0
                rep.tools.append({"tool": "terraform validate", "dir": reld, "ok": ok, "output": "" if ok else out[:4000]})
            else:
                rep.tools.append({"tool": "terraform init -backend=false", "dir": reld, "ok": False, "output": out[:4000]})
        else:
            rep.tools.append({"tool": "terraform", "dir": reld, "ok": None, "output": "not installed; fmt/validate skipped"})
        for tool, cmd, label in (("tflint", ["tflint", "--format", "compact", "--no-color"], "tflint"),
                                 ("trivy", ["trivy", "config", "--quiet", "--format", "table", "."], "trivy config")):
            if shutil.which(tool):
                rc, out = run_tool(cmd, d, timeout=600)
                rep.tools.append({"tool": label, "dir": reld, "ok": rc == 0, "output": out[:6000]})
            else:
                rep.tools.append({"tool": label, "dir": reld, "ok": None, "output": "not installed; skipped"})


# ----------------------------------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------------------------------
def load_config(path_hint, roots):
    cfg = dict(DEFAULT_CONFIG)
    candidates = [path_hint] if path_hint else []
    for r in roots:
        candidates += [os.path.join(r, "tfreview.json"), os.path.join(r, ".tfreview.json")]
    for c in candidates:
        if c and os.path.exists(c):
            with open(c) as f:
                cfg.update(json.load(f))
            cfg["_config_file"] = c
            break
    return cfg


def tf_dirs(paths):
    out = []
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isfile(p):
            out.append(os.path.dirname(p))
            continue
        for dirpath, dirnames, filenames in os.walk(p):
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
            if any(fn.endswith((".tf", ".tfvars")) for fn in filenames):
                out.append(dirpath)
    return sorted(dict.fromkeys(out))


def review(paths, config_path=None, tools=True, only_files=None):
    dirs = tf_dirs(paths)
    root = os.path.commonpath([os.path.abspath(p) for p in paths]) if paths else os.getcwd()
    if os.path.isfile(root):
        root = os.path.dirname(root)
    cfg = load_config(config_path, [root] + dirs)
    rep = Report(cfg)
    for d in dirs:
        files = {}
        for fn in sorted(os.listdir(d)):
            if not fn.endswith((".tf", ".tfvars")):
                continue
            if only_files and os.path.join(d, fn) not in only_files:
                continue
            with open(os.path.join(d, fn), "rb") as fh:
                src = fh.read()
            relf = os.path.relpath(os.path.join(d, fn), root)
            relf = relf[2:] if relf.startswith("./") else relf
            blk, err = parse_hcl(relf, src)
            files[fn] = (blk, err)
            for i, line in enumerate(src.decode("utf-8", "replace").splitlines(), 1):
                if ONE_LINE_BLOCK_RE.match(line):
                    rep.add("TF000", "high", relf, i, "single-line block with more than one attribute is invalid HCL (terraform will reject it)", "put each attribute on its own line")
            if err:
                rep.add("TF000", "high", relf, 1, "HCL parse error (tree-sitter); results for this file are partial", "run terraform fmt / validate to locate the syntax error")
        if not files:
            continue
        ctx = {"dir": d}
        check_directory(os.path.relpath(d, root) if d != root else ".", d, files, rep)
        for fn, (blk, _) in files.items():
            if fn.endswith(".tfvars"):
                for k, (expr, line) in blk.attrs.items():
                    lit = literal_string(expr)
                    if lit and SECRET_ATTR_RE.search(k):
                        rep.add("SEC021", "critical", blk.file, line, f'tfvars value "{k}" is a literal secret', "inject secrets via TF_VAR_* from the CI secret store, never in tfvars")
                    for pat, what in SECRET_PATTERNS:
                        if pat.search(expr):
                            rep.add("SEC021", "critical", blk.file, line, f"{what} in tfvars", "remove and rotate")
                continue
            for b in blk.blocks:
                check_block(b, rep, ctx)
    if tools and cfg.get("external_tools", True):
        external_tools(dirs, rep, root)
    # dedupe
    seen, uniq = set(), []
    for fnd in rep.findings:
        key = (fnd["rule"], fnd["file"], fnd["line"], fnd["message"])
        if key not in seen:
            seen.add(key)
            uniq.append(fnd)
    rep.findings = sorted(uniq, key=lambda x: (-SEVERITIES.index(x["severity"]), x["file"], x["line"]))
    return rep, root, dirs


def render(rep, root, dirs, fail_on):
    counts = defaultdict(int)
    for fnd in rep.findings:
        counts[fnd["severity"]] += 1
    lines = [f"## Terraform review: {os.path.basename(os.path.abspath(root)) or root}  ({len(dirs)} module dir(s))", ""]
    lines.append("Findings: " + ", ".join(f"{s} {counts[s]}" for s in reversed(SEVERITIES) if counts[s]) if rep.findings else "Findings: none")
    if rep.config.get("_config_file"):
        lines.append(f"Config: {os.path.relpath(rep.config['_config_file'])}")
    if rep.findings:
        lines += ["", "| severity | rule | location | finding | fix |", "|---|---|---|---|---|"]
        for fnd in rep.findings:
            lines.append(f"| {fnd['severity']} | {fnd['rule']} | {fnd['file']}:{fnd['line']} | {fnd['message']} | {fnd['fix'] or ''} |")
    if rep.tools:
        lines += ["", "### Tools", ""]
        for t in rep.tools:
            status = "ok" if t["ok"] else ("skipped" if t["ok"] is None else "FAILED")
            lines.append(f"- {t['tool']} [{t['dir']}]: {status}" + (f"\n```\n{t['output']}\n```" if t["output"] and t["ok"] is False else (f" ({t['output']})" if t["ok"] is None else "")))
    gate = SEVERITIES.index(fail_on)
    failing = [f for f in rep.findings if SEVERITIES.index(f["severity"]) >= gate] + [t for t in rep.tools if t["ok"] is False]
    lines += ["", f"Gate (--fail-on {fail_on}): " + ("FAIL" if failing else "PASS")]
    return "\n".join(lines), bool(failing)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=["."])
    ap.add_argument("--config", help="tfreview.json path (default: tfreview.json in the reviewed root)")
    ap.add_argument("--fail-on", default="high", choices=SEVERITIES)
    ap.add_argument("--no-tools", action="store_true", help="skip terraform/tflint/trivy even if installed")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--rules", action="store_true", help="list rule ids and exit")
    a = ap.parse_args(argv)
    if a.rules:
        src = open(__file__).read()
        ids = sorted(set(re.findall(r'rep\.add\("([A-Z]+\d+)", "(\w+)"', src)))
        for rid, sev in ids:
            print(f"{rid}\t{sev}")
        return 0
    rep, root, dirs = review(a.paths, a.config, tools=not a.no_tools)
    if a.json:
        gate = SEVERITIES.index(a.fail_on)
        print(json.dumps({"root": root, "dirs": [os.path.relpath(d, root) for d in dirs], "findings": rep.findings, "tools": rep.tools,
                          "fail": bool([f for f in rep.findings if SEVERITIES.index(f["severity"]) >= gate] or [t for t in rep.tools if t["ok"] is False])}, indent=1))
        return 0
    text, failing = render(rep, root, dirs, a.fail_on)
    print(text)
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
