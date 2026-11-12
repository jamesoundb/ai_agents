#!/usr/bin/env python3
"""Creates the GitLab release for CI_COMMIT_TAG, with a notes file as its description.

Calls the Releases API with the job's own CI_JOB_TOKEN. Standard library only, so the release job
runs in the same scanned Python image as every other job instead of a separate glab image (whose
base packages carried fixable critical vulnerabilities).

Usage: create_release.py [NOTES_FILE]   (default: release-notes.md)
Environment: CI_COMMIT_TAG, CI_API_V4_URL, CI_PROJECT_ID, CI_JOB_TOKEN (all set by GitLab CI).
"""
import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    notes_file = sys.argv[1] if len(sys.argv) > 1 else "release-notes.md"
    missing = [v for v in ("CI_COMMIT_TAG", "CI_API_V4_URL", "CI_PROJECT_ID", "CI_JOB_TOKEN") if not os.environ.get(v)]
    if missing:
        print(f"create_release: missing environment: {', '.join(missing)}", file=sys.stderr)
        return 2
    tag = os.environ["CI_COMMIT_TAG"]
    with open(notes_file, encoding="utf-8") as f:
        notes = f.read()

    url = f"{os.environ['CI_API_V4_URL']}/projects/{os.environ['CI_PROJECT_ID']}/releases"
    body = json.dumps({"tag_name": tag, "name": tag, "description": notes}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "JOB-TOKEN": os.environ["CI_JOB_TOKEN"],
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            release = json.load(resp)
    except urllib.error.HTTPError as e:
        # GitLab's error body says why (release exists, tag missing, token not allowed).
        detail = e.read().decode("utf-8", "replace")[:500]
        print(f"create_release: HTTP {e.code} from {url}: {detail}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"create_release: cannot reach {url}: {e.reason}", file=sys.stderr)
        return 1
    print(f"created release {release.get('tag_name', tag)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
