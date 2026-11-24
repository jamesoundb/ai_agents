#!/usr/bin/env python3
"""Tests for compass.py on the dungeon-map Gitea fixture (../dungeon-map/tests/fixture/gitea), in a
scratch ai-task workspace made by ../ai-task/scripts/ai-task init. Offline: "cloned" repositories are
local git repos whose origin is the fixture's URL. Stdlib only:  python3 tests/test_compass.py
"""
import contextlib
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS = os.path.normpath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(SKILLS, "compass", "scripts"))
import compass  # noqa: E402
import dungeonmap as dm  # noqa: E402  (compass put its directory on sys.path)

FIXTURE = os.path.join(SKILLS, "dungeon-map", "tests", "fixture", "gitea")
AI_TASK = os.path.join(SKILLS, "ai-task", "scripts", "ai-task")


def run(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            rc = compass.main(list(argv))
        except SystemExit as e:
            rc = e.code
    return rc, buf.getvalue()


class Compass(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="compass-test-")
        cls.cache = os.path.join(cls.tmp, "cache")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = dm.main(["--cache", cls.cache, "build", "localhost:3300/dmtest", "--provider", "gitea", "--from", FIXTURE])
        assert rc == 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="ws-", dir=self.tmp)
        subprocess.run([AI_TASK, "init"], cwd=self.ws, check=True, capture_output=True)
        self.cwd = os.getcwd()
        os.chdir(self.ws)

    def tearDown(self):
        os.chdir(self.cwd)

    def c(self, *argv):
        return run(*argv[:1], "--cache", self.cache, *argv[1:])

    def fake_clone(self, name, url):
        d = os.path.join(self.ws, name)
        subprocess.run(["git", "init", "-q", d], check=True)
        subprocess.run(["git", "-C", d, "remote", "add", "origin", url], check=True)

    # ---- queries
    def test_plan_proposes_the_matching_repos_with_reasons_and_related(self):
        rc, out = self.c("plan", "refunds fail for card payments in checkout")
        self.assertEqual(rc, 0, out)
        proposed = out.split("proposed (clone these):\n")[1].split("\n\n")[0]
        self.assertIn("dmtest/payments-api", proposed.splitlines()[0])
        self.assertIn("dmtest/checkout-web", proposed)
        self.assertIn("why: matches refunds (description)", proposed)
        related = out.split("related (")[1].split("\n\n")[0]
        self.assertIn("dmtest/payments-api depends on it: git submodule: .gitmodules:3", related)
        self.assertIn("dmtest/checkout-web depends on it: npm dependency: package.json:5", related)

    def test_archived_repos_are_not_proposed(self):
        rc, out = self.c("plan", "billing monolith")
        self.assertEqual(rc, 1)
        self.assertNotIn("proposed", out)
        self.assertIn("only archived repositories match: dmtest/legacy-billing", out)
        rc, out = self.c("find", "billing")
        self.assertIn("dmtest/legacy-billing [archived]", out)

    def test_show_both_directions(self):
        rc, out = self.c("show", "ui-kit", "platform-lib")
        self.assertEqual(rc, 0, out)
        self.assertIn("used by (1):\n    dmtest/checkout-web (npm dependency: package.json:5 in dmtest/checkout-web)", out)
        self.assertIn("used by (1):\n    dmtest/payments-api (Go module: go.mod:6 in dmtest/payments-api)", out)

    def test_find_by_topic_and_language(self):
        rc, out = self.c("find", "--topic", "helm")
        self.assertEqual([l.split(" ")[0] for l in out.splitlines()], ["dmtest/infra-live", "dmtest/service-chart"])
        rc, out = self.c("find", "--language", "HCL")
        self.assertEqual(len(out.splitlines()), 2, out)

    def test_cache_flag_before_the_command_like_dungeon_map(self):
        rc, out = run("--cache", self.cache, "show", "ui-kit")
        self.assertEqual(rc, 0, out)
        self.assertIn("dmtest/ui-kit", out)

    def test_unknown_and_ambiguous_names(self):
        rc, out = self.c("show", "nope")
        self.assertEqual(rc, 1)
        self.assertIn("no repository 'nope' in the map", out)

    # ---- handoff to ai-task
    def test_handoff_skips_present_clones_and_flags_a_basename_clash(self):
        self.fake_clone("payments-api", "http://localhost:3300/dmtest/payments-api.git")
        self.fake_clone("proto", "https://github.com/someone-else/proto.git")
        rc, out = self.c("handoff", "payments-api", "proto", "ui-kit", "--slug", "fix-refunds")
        self.assertEqual(rc, 0, out)
        self.assertIn("have     dmtest/payments-api -> payments-api/", out)
        self.assertIn("CLASH    dmtest/proto: proto/ already holds another repository (github.com/someone-else/proto)", out)
        nxt = out.split("next (one command per call):\n")[1].splitlines()
        self.assertEqual(nxt[0], "  .ai/bin/ai-task clone http://localhost:3300/dmtest/ui-kit.git")
        self.assertTrue(nxt[1].endswith("run.sh seed dmtest/ui-kit"), nxt[1])
        self.assertEqual(nxt[2], "  .ai/bin/ai-task start fix-refunds payments-api ui-kit")

    def test_handoff_from_a_subdirectory_names_the_root(self):
        os.makedirs(os.path.join(self.ws, "sub"))
        os.chdir(os.path.join(self.ws, "sub"))
        rc, out = self.c("handoff", "ui-kit")
        self.assertIn(f"(run these from the workspace root {os.path.realpath(self.ws)})", out.replace(self.ws, os.path.realpath(self.ws)))
        self.assertIn(os.path.join(".ai", "bin", "ai-task") + " clone", out)
        self.assertIn(f"{compass.RUN} seed dmtest/ui-kit", out)

    def test_handoff_without_a_workspace(self):
        bare = tempfile.mkdtemp(dir=self.tmp)
        rc, out = self.c("handoff", "ui-kit", "--workspace", bare)
        self.assertEqual(rc, 1)
        self.assertIn("no ai-task workspace", out)

    def test_clone_spec(self):
        gh = {"host": "github.com", "path": "acme/api", "clone_https": "https://github.com/acme/api.git",
              "clone_ssh": "git@github.com:acme/api.git", "web_url": ""}
        self.assertEqual(compass.clone_spec(gh), "acme/api")  # ai-task resolves owner/name on github.com
        self.assertEqual(compass.clone_spec(gh, ssh=True), "git@github.com:acme/api.git")
        gl = dict(gh, host="gitlab.com", path="acme/sub/api", clone_https="https://gitlab.com/acme/sub/api.git")
        self.assertEqual(compass.clone_spec(gl), "https://gitlab.com/acme/sub/api.git")

    # ---- seed
    def test_seed_writes_a_block_and_rewrites_it_in_place(self):
        ref = os.path.join(self.ws, ".ai", "instructions", "reference", "payments-api.md")
        with open(ref, "w") as f:
            f.write("# payments-api\n\nLiving reference: architecture, setup, gotchas. Not a worklog.\n\n## Gotchas\n\n- mine\n")
        rc, out = self.c("seed", "payments-api")
        self.assertEqual(rc, 0, out)
        rc, out = self.c("seed", "payments-api")
        with open(ref) as f:
            text = f.read()
        self.assertEqual(text.count(compass.BEGIN), 1)
        self.assertIn("- mine\n", text)
        self.assertIn("- Depends on: dmtest/proto (git submodule: .gitmodules:3)", text)
        self.assertTrue(text.startswith("# payments-api\n"))

    def test_seed_before_clone_uses_ai_task_header(self):
        rc, out = self.c("seed", "ui-kit")
        with open(os.path.join(self.ws, ".ai", "instructions", "reference", "ui-kit.md")) as f:
            text = f.read()
        self.assertTrue(text.startswith("# ui-kit\n\nLiving reference: architecture, setup, gotchas. Not a worklog.\n"))
        self.assertIn("- Used by: dmtest/checkout-web", text)

    # ---- map missing or stale
    def test_missing_map_points_to_dungeon_map(self):
        rc, out = run("status", "--cache", os.path.join(self.tmp, "empty"))
        self.assertEqual(rc, 1)
        self.assertIn("no dungeon map", out)
        self.assertIn("dungeon-map/scripts/run.sh build https://github.com/ORG", out)

    def test_stale_map_is_reported_with_the_rebuild_command(self):
        cache = os.path.join(self.tmp, "stale")
        shutil.copytree(self.cache, cache)
        db = sqlite3.connect(dm.graph_path("localhost:3300", "dmtest", cache))
        db.execute("UPDATE meta SET value='2026-01-01T00:00:00Z' WHERE key='built_at'")
        db.commit()
        db.close()
        rc, out = run("plan", "payments", "--cache", cache)
        self.assertRegex(out, r"stale: localhost:3300/dmtest was built \d+ days ago; .* build localhost:3300/dmtest --provider gitea")


if __name__ == "__main__":
    unittest.main(verbosity=1)
