#!/usr/bin/env python3
"""Tests for create_release.py against a local stand-in for the GitLab Releases API. No network."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "create_release.py")


class FakeGitLab(BaseHTTPRequestHandler):
    """Records each request; answers with the status the test sets on the server."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.server.requests.append({
            "path": self.path,
            "token": self.headers.get("JOB-TOKEN"),
            "type": self.headers.get("Content-Type"),
            "body": json.loads(self.rfile.read(length)),
        })
        status, payload = self.server.reply
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, *args):  # keep test output quiet
        pass


class CreateReleaseTest(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), FakeGitLab)
        self.server.requests = []
        self.server.reply = (201, {"tag_name": "v1.2.0"})
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.notes = os.path.join(self.tmp.name, "release-notes.md")
        with open(self.notes, "w", encoding="utf-8") as f:
            f.write("## Changes\n\n- first \"quoted\" change\n")
        self.env = dict(os.environ, CI_COMMIT_TAG="v1.2.0", CI_PROJECT_ID="42", CI_JOB_TOKEN="job-token",
                        CI_API_V4_URL=f"http://127.0.0.1:{self.server.server_port}/api/v4")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def run_script(self, env):
        return subprocess.run([sys.executable, SCRIPT, self.notes], env=env, capture_output=True, text=True)

    def test_posts_the_release_with_the_job_token(self):
        r = self.run_script(self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        [req] = self.server.requests
        self.assertEqual(req["path"], "/api/v4/projects/42/releases")
        self.assertEqual(req["token"], "job-token")
        self.assertEqual(req["type"], "application/json")
        self.assertEqual(req["body"], {"tag_name": "v1.2.0", "name": "v1.2.0",
                                       "description": "## Changes\n\n- first \"quoted\" change\n"})

    def test_an_api_error_fails_with_gitlabs_reason(self):
        self.server.reply = (409, {"message": "Release already exists"})
        r = self.run_script(self.env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("HTTP 409", r.stderr)
        self.assertIn("Release already exists", r.stderr)

    def test_missing_ci_variables_fail_before_any_request(self):
        env = dict(self.env)
        del env["CI_JOB_TOKEN"]
        r = self.run_script(env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("CI_JOB_TOKEN", r.stderr)
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main()
