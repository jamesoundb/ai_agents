#!/usr/bin/env python3
"""seed_gitea.py -- create the test org (tests/org/) on a Gitea instance through `gt api`.

  python3 tests/seed_gitea.py --host localhost:3000 --org dmtest

For a throwaway instance only (see tests/gitea.sh): it creates the org, one repository per folder
of tests/org/ with the description, topics and archived flag from tests/org/repos.json, and the
folder's files as the first commit. {HOST} and {ORG} in file contents become --host and --org.
gt holds the login; this script never sees a token.
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def gt(host, method, endpoint, body=None):
    argv = ["gt", "--no-input", "--host", host, "api", "-X", method, endpoint]
    tmp = None
    if body is not None:
        fd, tmp = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(body, f)
        argv += ["--input", tmp, "-H", "Content-Type: application/json"]
    try:
        r = subprocess.run(argv, capture_output=True, text=True)
    finally:
        if tmp:
            os.unlink(tmp)
    if r.returncode != 0:
        raise SystemExit(f"gt api -X {method} {endpoint}: {r.stderr.strip() or r.stdout.strip()}")
    return json.loads(r.stdout) if r.stdout.strip() else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", required=True)
    ap.add_argument("--org", required=True)
    ap.add_argument("--fixture", default=os.path.join(HERE, "org"))
    a = ap.parse_args()
    meta = json.load(open(os.path.join(a.fixture, "repos.json"), encoding="utf-8"))
    gt(a.host, "POST", "orgs", {"username": a.org, "visibility": "public"})
    for repo in sorted(meta):
        m = meta[repo]
        gt(a.host, "POST", f"orgs/{a.org}/repos", {"name": repo, "description": m["description"],
                                                  "default_branch": "main", "auto_init": False})
        files = []
        root = os.path.join(a.fixture, repo)
        for d, _, names in os.walk(root):
            for n in sorted(names):
                p = os.path.join(d, n)
                text = open(p, encoding="utf-8").read().replace("{HOST}", a.host).replace("{ORG}", a.org)
                files.append({"operation": "create", "path": os.path.relpath(p, root),
                              "content": base64.b64encode(text.encode()).decode()})
        gt(a.host, "POST", f"repos/{a.org}/{repo}/contents", {"branch": "main", "message": "initial", "files": files})
        gt(a.host, "PUT", f"repos/{a.org}/{repo}/topics", {"topics": m["topics"]})
        if m.get("archived"):
            gt(a.host, "PATCH", f"repos/{a.org}/{repo}", {"archived": True})
        print(f"{a.org}/{repo}: {len(files)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
