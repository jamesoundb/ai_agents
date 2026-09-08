#!/usr/bin/env python3
"""
scaffold.py — generate Terraform skeletons that follow Google's standard module structure and the
company conventions baked into ../templates (edit those templates to change the conventions).

  scaffold.py module NAME [--dir modules/NAME] [--description TEXT] [--module-source SRC]
  scaffold.py root ENV --service NAME --state-bucket BUCKET --project-id ID [--region R] [--owner TEAM]
                       [--dir environments/ENV] [--module-source ../../modules/NAME]
Common: --google-provider-version 5.0  --force (overwrite existing files)  --dry-run
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(os.path.dirname(HERE), "templates")


def render_tree(kind, dest, vars_, force, dry_run):
    src_root = os.path.join(TEMPLATES, kind)
    written, skipped = [], []
    for dirpath, _, filenames in os.walk(src_root):
        for fn in sorted(filenames):
            src = os.path.join(dirpath, fn)
            rel = os.path.relpath(src, src_root)
            out = os.path.join(dest, rel)
            content = open(src, encoding="utf-8").read()
            for k, v in vars_.items():
                content = content.replace("{{" + k + "}}", v)
            if os.path.exists(out) and not force:
                skipped.append(out)
                continue
            if not dry_run:
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with open(out, "w", encoding="utf-8") as f:
                    f.write(content)
            written.append(out)
    return written, skipped


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="kind", required=True)
    m = sub.add_parser("module")
    m.add_argument("name")
    m.add_argument("--dir")
    m.add_argument("--description", default="Terraform module for Google Cloud resources.")
    m.add_argument("--module-source", default=None, help="source string shown in README usage (default ../../modules/NAME)")
    r = sub.add_parser("root")
    r.add_argument("env")
    r.add_argument("--service", required=True)
    r.add_argument("--state-bucket", required=True)
    r.add_argument("--project-id", required=True)
    r.add_argument("--region", default="us-central1")
    r.add_argument("--owner", default="platform")
    r.add_argument("--dir")
    r.add_argument("--module-source", default=None)
    for p in (m, r):
        p.add_argument("--google-provider-version", default="5.0")
        p.add_argument("--force", action="store_true")
        p.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    if a.kind == "module":
        name = a.name
        dest = a.dir or os.path.join("modules", name)
        vars_ = {"name": name, "name_underscore": name.replace("-", "_"), "description": a.description,
                 "module_source": a.module_source or f"../../modules/{name}", "region": "us-central1",
                 "google_provider_version": a.google_provider_version}
    else:
        dest = a.dir or os.path.join("environments", a.env)
        vars_ = {"env": a.env, "service": a.service, "service_underscore": a.service.replace("-", "_"),
                 "state_bucket": a.state_bucket, "project_id": a.project_id, "region": a.region, "owner": a.owner,
                 "module_source": a.module_source or f"../../modules/{a.service}",
                 "google_provider_version": a.google_provider_version}
    written, skipped = render_tree(a.kind, dest, vars_, a.force, a.dry_run)
    for w in written:
        print(("would write " if a.dry_run else "wrote ") + w)
    for s in skipped:
        print("kept existing " + s + " (use --force to overwrite)")
    print(f"\nnext: cd {dest} && terraform fmt -recursive && terraform init -backend=false && terraform validate; then run the terraform-review skill.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
