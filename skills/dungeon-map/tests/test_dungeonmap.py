#!/usr/bin/env python3
"""Tests for dungeonmap.py. Offline: the Gitea case replays tests/fixture/gitea/responses.json (a
--save of tests/gitea.sh's throwaway instance seeded with tests/org/); GitHub and GitLab answers
are built here in the shape of their APIs. Stdlib only:  python3 tests/test_dungeonmap.py
"""
import contextlib
import copy
import io
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import dungeonmap as dm  # noqa: E402

GITEA = json.load(open(os.path.join(HERE, "fixture", "gitea", "responses.json"), encoding="utf-8"))
# Every edge planted in tests/org/ (src, dst, via); nothing else may resolve (the decoys: react,
# numpy, github.com/google/uuid, bitnami redis, the dns registry module, golang/debian images).
PLANTED = {
    ("payments-api", "platform-lib", "go"), ("payments-api", "proto", "submodule"),
    ("payments-api", "base-image", "image"), ("payments-api", "ci-actions", "action"),
    ("checkout-web", "ui-kit", "npm"), ("ml-pipeline", "feature-store-client", "python"),
    ("infra-live", "service-chart", "helm"), ("infra-live", "terraform-google-network", "terraform"),
}

# The org's own references that no repository provides (src, ident): each must be reported by `gaps`.
# Look-alike decoys that must not be: @dmtest-labs/analytics (another npm scope), dmtest-analytics
# (unscoped npm: the org publishes none), github.com/dmtest/billing-sdk (the same path on another
# host), and the public packages and images above. dmtest-metrics shares the dmtest- name of two of
# the org's PyPI packages: PyPI has no namespaces, so it is reported only with --internal pypi:dmtest-
# (which `gaps` suggests), and dmtestlib never is.
UNRESOLVED = {
    ("checkout-web", "@dmtest/analytics"), ("payments-api", "localhost/dmtest/billing-sdk"),
    ("infra-live", "dmtest/dns/google"),
}


def run(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            rc = dm.main(list(argv))
        except SystemExit as e:
            rc = e.code
    return rc, buf.getvalue()


def edges(m, host=None):
    return {(e["src"].rsplit("/", 1)[1], e["dst"].rsplit("/", 1)[1], e["via"]) for e in m.edges
            if not host or e["src"].startswith(host)}


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dungeonmap-test-")
        self.cache = os.path.join(self.tmp, "cache")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def replay(self, responses, target, provider):
        d = os.path.join(self.tmp, "rec")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "responses.json"), "w") as f:
            json.dump(responses, f)
        return run("--cache", self.cache, "build", target, "--provider", provider, "--from", d)


class GiteaFixture(Tmp):
    def test_every_planted_edge_and_no_decoy(self):
        rc, out = self.replay(GITEA, "localhost:3300/dmtest", "gitea")
        self.assertEqual(rc, 0, out)
        self.assertIn("14 repos (14 scanned, 0 unchanged, 0 failed, 1 incomplete)", out)
        m = dm.load_map(self.cache)
        self.assertEqual(edges(m), PLANTED)

    def test_metadata(self):
        self.replay(GITEA, "localhost:3300/dmtest", "gitea")
        m = dm.load_map(self.cache)
        r = m.repos["localhost:3300/dmtest/payments-api"]
        self.assertEqual(r["topics"], ["go", "payments", "service"])
        self.assertEqual(r["default_branch"], "main")
        self.assertEqual(r["readme"], "The payments service: card charges, refunds and the ledger API used by checkout.")
        self.assertTrue(m.repos["localhost:3300/dmtest/legacy-billing"]["archived"])
        e = [e for e in m.edges if e["via"] == "go"][0]
        self.assertEqual((e["file"], e["line"]), ("go.mod", 6))

    def test_incremental_rebuild_reads_only_changed_repos(self):
        self.replay(GITEA, "localhost:3300/dmtest", "gitea")
        changed = copy.deepcopy(GITEA)
        listing = changed["json orgs/dmtest/repos?limit=50&page=1"]
        for r in listing:
            if r["name"] == "ui-kit":
                r["updated_at"] = "2030-01-01T00:00:00Z"
        rc, out = self.replay(changed, "localhost:3300/dmtest", "gitea")
        self.assertEqual(rc, 0, out)
        self.assertIn("14 repos (1 scanned, 13 unchanged, 0 failed, 1 incomplete)", out)
        self.assertEqual(edges(dm.load_map(self.cache)), PLANTED)  # reused rows keep their edges
        rc, out = self.replay(changed, "localhost:3300/dmtest", "gitea")
        self.assertIn("(0 scanned, 14 unchanged", out)

    def test_no_credentials_in_the_map(self):
        self.replay(GITEA, "localhost:3300/dmtest", "gitea")
        db = sqlite3.connect(dm.graph_path("localhost:3300", "dmtest", self.cache))
        cols = {t: [c[1] for c in db.execute(f"PRAGMA table_info({t})")] for t in ("repos", "meta")}
        self.assertFalse([c for cs in cols.values() for c in cs if "token" in c or "auth" in c])
        dump = "\n".join(db.iterdump())
        # Gitea tokens are 40 hex characters; GitHub and GitLab tokens have fixed prefixes.
        self.assertNotRegex(dump, r"\b[0-9a-f]{40}\b|gh[pousr]_[A-Za-z0-9]|github_pat_|glpat-")

    def test_maps_lists_the_graph(self):
        self.replay(GITEA, "localhost:3300/dmtest", "gitea")
        rc, out = run("--cache", self.cache, "maps")
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"localhost:3300/dmtest\s+gitea\s+14 repos\s+8 edges out.*gaps: 1 incomplete, 3 unresolved")


def gh_repo(name, **kw):
    r = {"full_name": f"acme/{name}", "name": name, "description": f"{name} service", "topics": [],
         "language": "Go", "default_branch": "main", "archived": False, "fork": False,
         "pushed_at": "2026-11-01T00:00:00Z", "html_url": f"https://github.com/acme/{name}",
         "clone_url": f"https://github.com/acme/{name}.git", "ssh_url": f"git@github.com:acme/{name}.git", "size": 10}
    r.update(kw)
    return r


class GitHubShape(Tmp):
    def responses(self):
        return {
            "json orgs/acme/repos?type=all&per_page=100&page=1": [gh_repo("api"), gh_repo("workflows"), gh_repo("empty", size=0)],
            "json orgs/acme/repos?type=all&per_page=100&page=2": [],
            "json repos/acme/api/git/trees/main?recursive=1": {"tree": [
                {"path": "README.md", "type": "blob", "size": 50},
                {"path": ".github/workflows/ci.yml", "type": "blob", "size": 50},
                {"path": "node_modules/x/package.json", "type": "blob", "size": 50}], "truncated": False},
            "raw repos/acme/api/contents/README.md?ref=main": "# api\n\nThe public API gateway for every acme service.\n",
            "raw repos/acme/api/contents/.github/workflows/ci.yml?ref=main":
                "jobs:\n  build:\n    uses: acme/workflows/.github/workflows/go.yml@main\n",
            "json repos/acme/workflows/git/trees/main?recursive=1": {"tree": [], "truncated": False},
        }

    def test_reusable_workflow_edge_and_skips(self):
        rc, out = self.replay(self.responses(), "https://github.com/acme", "github")
        self.assertEqual(rc, 0, out)
        m = dm.load_map(self.cache)
        self.assertEqual(edges(m), {("api", "workflows", "workflow")})
        self.assertEqual(m.repos["github.com/acme/empty"]["note"], "empty repository")
        self.assertNotIn("node_modules/x/package.json", m.repos["github.com/acme/api"]["files"])

    def test_user_account_fallback(self):
        r = self.responses()
        r["json users/acme/repos?type=owner&per_page=100&page=1"] = r.pop("json orgs/acme/repos?type=all&per_page=100&page=1")
        r["json users/acme/repos?type=owner&per_page=100&page=2"] = []
        rc, out = self.replay(r, "acme", "github")
        self.assertEqual(rc, 0, out)
        self.assertIn("3 repos", out)


def gl_project(pid, path, **kw):
    ns, name = path.rsplit("/", 1)
    p = {"id": pid, "path_with_namespace": path, "path": name, "description": "", "topics": ["x"],
         "default_branch": "main", "archived": False, "last_activity_at": "2026-11-01T00:00:00Z",
         "web_url": f"https://gitlab.example.com/{path}", "http_url_to_repo": f"https://gitlab.example.com/{path}.git",
         "ssh_url_to_repo": f"git@gitlab.example.com:{path}.git", "empty_repo": False}
    p.update(kw)
    return p


class GitLabShape(Tmp):
    def responses(self):
        q = dm.q
        return {
            f"json groups/{q('acme')}/projects?include_subgroups=false&with_shared=false&per_page=100&page=1":
                [gl_project(1, "acme/app")],
            f"json groups/{q('acme')}/projects?include_subgroups=false&with_shared=false&per_page=100&page=2": [],
            f"json groups/{q('acme')}/subgroups?per_page=100&page=1": [{"full_path": "acme/platform"}],
            f"json groups/{q('acme')}/subgroups?per_page=100&page=2": [],
            f"json groups/{q('acme/platform')}/projects?include_subgroups=false&with_shared=false&per_page=100&page=1":
                [gl_project(2, "acme/platform/ci-templates"), gl_project(3, "acme/platform/deploy")],
            f"json groups/{q('acme/platform')}/projects?include_subgroups=false&with_shared=false&per_page=100&page=2": [],
            f"json groups/{q('acme/platform')}/subgroups?per_page=100&page=1": [],
            "json projects/1/languages": {"Python": 70.0, "Shell": 30.0},
            "json projects/2/languages": {},
            "json projects/3/languages": {"Go": 100.0},
            "json projects/1/repository/tree?recursive=true&ref=main&per_page=100&page=1":
                [{"path": ".gitlab-ci.yml", "type": "blob"}, {"path": "src", "type": "tree"}],
            "json projects/1/repository/tree?recursive=true&ref=main&per_page=100&page=2": [],
            "raw projects/1/repository/files/.gitlab-ci.yml/raw?ref=main": (
                "include:\n  - project: 'acme/platform/ci-templates'\n    file: '/python.yml'\n"
                "  - component: $CI_SERVER_FQDN/acme/platform/ci-templates/lint@1.0\n"
                "deploy:\n  trigger:\n    project: $CI_PROJECT_NAMESPACE/platform/deploy\n"),
            "json projects/2/repository/tree?recursive=true&ref=main&per_page=100&page=1": [],
            "json projects/3/repository/tree?recursive=true&ref=main&per_page=100&page=1": [],
        }

    def test_subgroups_are_graphs_and_ci_edges_cross_them(self):
        rc, out = self.replay(self.responses(), "gitlab.example.com/acme", "gitlab")
        self.assertEqual(rc, 0, out)
        m = dm.load_map(self.cache)
        self.assertEqual(set(m.graphs), {"gitlab.example.com/acme", "gitlab.example.com/acme/platform"})
        self.assertEqual(m.graphs["gitlab.example.com/acme/platform"]["parent"], "acme")
        self.assertEqual(edges(m), {("app", "ci-templates", "ci-include"), ("app", "ci-templates", "ci-component"),
                                    ("app", "deploy", "ci-trigger")})
        self.assertEqual(m.repos["gitlab.example.com/acme/app"]["language"], "Python")

    def test_no_subgroups(self):
        rc, out = self.replay(self.responses(), "gitlab.example.com/acme", "gitlab")
        shutil.rmtree(self.cache)
        d = os.path.join(self.tmp, "rec")
        rc, out = run("--cache", self.cache, "build", "gitlab.example.com/acme", "--provider", "gitlab",
                      "--from", d, "--no-subgroups")
        self.assertEqual(rc, 0, out)
        self.assertIn("1 subgroups not built (--no-subgroups): acme/platform", out)
        self.assertEqual(set(dm.load_map(self.cache).graphs), {"gitlab.example.com/acme"})


class CrossGraph(Tmp):
    def test_edge_between_two_providers(self):
        """A GitHub repo whose go.mod requires a module that lives on a self-hosted GitLab."""
        gl = GitLabShape.responses(None)
        gl["json projects/1/repository/tree?recursive=true&ref=main&per_page=100&page=1"] = []
        self.replay(gl, "gitlab.example.com/acme", "gitlab")
        gh = GitHubShape.responses(None)
        gh["json repos/acme/api/git/trees/main?recursive=1"]["tree"] = [{"path": "go.mod", "type": "blob", "size": 9}]
        gh["raw repos/acme/api/contents/go.mod?ref=main"] = (
            "module github.com/acme/api\n\nrequire gitlab.example.com/acme/platform/deploy/pkg/client v0.3.0\n")
        self.replay(gh, "github.com/acme", "github")
        m = dm.load_map(self.cache)
        e = [e for e in m.edges if e["src"] == "github.com/acme/api"]
        self.assertEqual([(x["dst"], x["via"], x["match"]) for x in e],
                         [("gitlab.example.com/acme/platform/deploy", "go", "prefix")])


def gaps_of(m, name, host="localhost:3300/dmtest"):
    return {g["kind"]: g for g in m.repos[f"{host}/{name}"]["gaps"]}


class Completeness(Tmp):
    """Nothing the scan left out may go unreported: every limit is a gap, and every reference in the
    org's own namespaces that resolves to no repository is listed with its file:line."""

    def build(self, *extra, responses=None):
        d = os.path.join(self.tmp, "rec")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "responses.json"), "w") as f:
            json.dump(responses or GITEA, f)
        return run("--cache", self.cache, "build", "localhost:3300/dmtest", "--provider", "gitea", "--from", d, *extra)

    def test_internal_references_with_no_repository_and_no_decoy(self):
        rc, out = self.build()
        self.assertEqual(rc, 0, out)
        self.assertIn("gaps: 1 repositories read incompletely, 3 internal references with no repository", out)
        m = dm.load_map(self.cache)
        self.assertEqual({(u["src"].rsplit("/", 1)[1], u["ident"]) for u in m.unresolved}, UNRESOLVED)
        self.assertEqual(edges(m), PLANTED)  # the new references and decoys add no edge
        self.assertEqual(m.internal_hints, [("pypi", "dmtest-", 2)])

    def test_a_shared_package_name_is_internal_only_when_declared(self):
        self.build()
        rc, out = run("--cache", self.cache, "gaps")
        self.assertIn("note: 2 of the org's pypi packages are named dmtest-*", out)
        self.assertIn("rebuild with --internal pypi:dmtest-", out)
        self.build("--internal", "pypi:dmtest-")
        m = dm.load_map(self.cache)
        got = {(u["src"].rsplit("/", 1)[1], u["ident"]) for u in m.unresolved}
        self.assertEqual(got, UNRESOLVED | {("ml-pipeline", "dmtest-metrics")})  # not dmtestlib
        u = [u for u in m.unresolved if u["ident"] == "dmtest-metrics"][0]
        self.assertEqual((u["file"], u["line"], u["why"]), ("pyproject.toml", 5, "internal prefix dmtest- (build --internal)"))
        self.assertEqual(m.internal_hints, [])

    def test_oversized_manifest_is_a_gap(self):
        self.build()
        g = gaps_of(dm.load_map(self.cache), "big-monorepo")
        self.assertEqual(set(g), {"file-too-large"})
        self.assertEqual(g["file-too-large"]["detail"], "packages/huge/package.json")

    def test_manifest_cap_is_a_gap_with_the_counts(self):
        rc, out = self.build("--max-files", "4")
        self.assertEqual(rc, 0, out)
        g = gaps_of(dm.load_map(self.cache), "big-monorepo")["manifest-cap"]
        self.assertEqual((g["found"], g["read"]), (8, 4))
        self.assertEqual(dm.gap_text(g), "read 4 of 8 manifests (--max-files 4)")

    def test_tree_page_limit_is_a_gap(self):
        # recorded with DM_GITEA_TREE_PAGE=5: big-monorepo's 21 entries take 5 pages
        self.build("--max-tree-entries", "5")
        g = gaps_of(dm.load_map(self.cache), "big-monorepo")["tree-truncated"]
        self.assertEqual(g["detail"], "tree listing stopped at 5 of 21 entries (--max-tree-entries 5)")
        rc, out = self.build()  # the default reads all 5 pages
        self.assertNotIn("tree-truncated", gaps_of(dm.load_map(self.cache), "big-monorepo"))

    def test_github_truncated_tree_is_a_gap(self):
        r = GitHubShape.responses(None)
        r["json repos/acme/api/git/trees/main?recursive=1"]["truncated"] = True
        self.replay(r, "https://github.com/acme", "github")
        g = gaps_of(dm.load_map(self.cache), "api", host="github.com/acme")["tree-truncated"]
        self.assertIn("GitHub truncated the tree listing", dm.gap_text(g))

    def test_gitlab_tree_page_limit_is_a_gap(self):
        class T:
            calls = 0

            def json(self, endpoint):
                return [{"path": f"f{i}", "type": "blob"} for i in range(100)]
        files, cut = dm.GitLab(T(), "gitlab.example.com").tree({"_id": 1, "default_branch": "main"}, max_entries=200)
        self.assertEqual((len(files), cut), (200, "tree listing stopped at 200 entries (--max-tree-entries 200)"))

    def test_failed_scan_is_a_gap(self):
        r = copy.deepcopy(GITEA)
        del r["json repos/dmtest/ui-kit/git/trees/main?recursive=true&per_page=1000&page=1"]
        r["json repos/dmtest/ui-kit/git/trees/main?recursive=true&per_page=1000&page=1"] = None
        rc, out = self.build(responses=r)
        self.assertIn("1 failed", out)
        self.assertEqual(set(gaps_of(dm.load_map(self.cache), "ui-kit")), {"scan-failed"})

    def test_scanner_version_change_rescans_every_repository(self):
        self.build()
        rc, out = self.build()
        self.assertIn("(0 scanned, 14 unchanged", out)
        old = dm.SCANNER
        try:
            dm.SCANNER = old + 1
            rc, out = self.build()
        finally:
            dm.SCANNER = old
        self.assertIn(f"scanner {old} -> {old + 1}: rescanning every repository", out)
        self.assertIn("(14 scanned, 0 unchanged", out)

    def test_a_map_from_before_scanner_versions_is_rescanned(self):
        self.build()
        db = sqlite3.connect(dm.graph_path("localhost:3300", "dmtest", self.cache))
        db.execute("DELETE FROM meta WHERE key='scanner'")
        db.execute("DROP TABLE gaps")
        db.commit()
        db.close()
        self.assertEqual(dm.load_map(self.cache).repos["localhost:3300/dmtest/big-monorepo"]["gaps"], [])
        rc, out = self.build()
        self.assertIn(f"scanner 1 -> {dm.SCANNER}", out)
        self.assertIn("(14 scanned, 0 unchanged", out)

    def test_reused_repositories_keep_their_gaps(self):
        self.build()
        self.build()
        self.assertIn("file-too-large", gaps_of(dm.load_map(self.cache), "big-monorepo"))

    def test_internal_prefixes_are_stored_and_kept(self):
        rc, out = self.build("--internal", "npm:dmtest-")
        self.assertIn("4 internal references", out)  # dmtest-analytics is now the org's
        self.build()
        self.assertIn(("checkout-web", "dmtest-analytics"),
                      {(u["src"].rsplit("/", 1)[1], u["ident"]) for u in dm.load_map(self.cache).unresolved})
        rc, out = self.build("--internal", "nuget:Acme.")
        self.assertEqual(rc, 2)
        self.assertIn("--internal takes NS:PREFIX", out)

    def test_gaps_command(self):
        self.build("--max-files", "4")
        rc, out = run("--cache", self.cache, "gaps")
        self.assertEqual(rc, 0)
        self.assertIn("dmtest/big-monorepo: read 4 of 8 manifests (--max-files 4)", out)
        self.assertIn("dmtest/payments-api -> localhost/dmtest/billing-sdk (go, go.mod:7; under localhost/dmtest/)", out)
        rc, out = run("--cache", self.cache, "gaps", "ui-kit")
        self.assertIn("no gaps", out)
        rc, out = run("--cache", self.cache, "gaps", "nope")
        self.assertEqual(rc, 1)


class InternalRules(unittest.TestCase):
    def rules(self, provides, forks=(), org="acme"):
        m = dm.Map()
        m.graphs[f"github.com/{org}"] = {"host": "github.com", "org": org}
        prov = {}
        for key, ns, ident in provides:
            m.repos[key] = {"fork": key in forks}
            prov.setdefault((ns, ident), set()).add(key)
        return dm.internal_rules(m, prov)

    def test_namespaces_from_what_the_org_provides(self):
        f, hints = self.rules([("github.com/acme/a", "go", "go.acme.dev/a"), ("github.com/acme/b", "maven", "com.acme:b"),
                               ("github.com/acme/c", "npm", "@acme/c"), ("github.com/acme/d", "pypi", "acme-d"),
                               ("github.com/acme/e", "pypi", "acme-e"), ("github.com/acme/f", "go", "github.com/acme/f")])
        for ns, ident in (("go", "go.acme.dev/x"), ("go", "github.com/acme/x"), ("git", "github.com/acme/x"),
                          ("maven", "com.acme:x"), ("maven", "com.acme.billing:x"), ("npm", "@acme/x"),
                          ("image", "acme/x")):
            self.assertTrue(f(ns, ident), (ns, ident))
        for ns, ident in (("go", "github.com/acmecorp/x"), ("go", "github.com/other/x"), ("maven", "com.acmex:y"),
                          ("npm", "@acme-labs/x"), ("npm", "acme-x"), ("pypi", "acme_x"), ("pypi", "acmex"),
                          ("image", "library/acme")):
            self.assertFalse(f(ns, ident), (ns, ident))
        self.assertEqual(hints, [("pypi", "acme-", 2)])

    def test_shared_names_are_suggested_only_when_they_are_the_orgs_name(self):
        """Found live on open-telemetry: a conformance repository's google-adk-* and llama-index-*
        packages made google-genai and llama-index-core look internal."""
        provides = [("github.com/open-telemetry/c", "pypi", n) for n in
                    ("google-adk-conformance", "google-genai-conformance", "opentelemetry-sdk", "opentelemetry-api")]
        f, hints = self.rules(provides, org="open-telemetry")
        self.assertEqual(hints, [("pypi", "opentelemetry-", 2)])
        self.assertFalse(f("pypi", "google-genai"))
        self.assertFalse(f("pypi", "opentelemetry-instrumentation-openai"))  # traceloop's, on PyPI

    def test_a_fork_does_not_make_its_upstream_internal(self):
        f, _ = self.rules([("github.com/acme/k8s", "go", "k8s.io/kubernetes")], forks={"github.com/acme/k8s"})
        self.assertFalse(f("go", "k8s.io/client-go"))


class FilePlan(unittest.TestCase):
    def test_kinds_take_turns_under_the_cap(self):
        """700 build.gradle files cannot crowd out the Dockerfile and the workflow."""
        files = [(f"instrumentation/m{i:03}/build.gradle.kts", 10) for i in range(700)]
        files += [(".github/workflows/ci.yml", 10), ("deploy/docker/app/Dockerfile", 10),
                  ("instrumentation/m000/sub/build.gradle.kts", 10)]
        picked, found, large = dm.plan_files(files, max_files=10)
        paths = [p for p, _ in picked]
        self.assertEqual((found, len(picked), large), (703, 10, []))
        self.assertIn(".github/workflows/ci.yml", paths)
        self.assertIn("deploy/docker/app/Dockerfile", paths)
        self.assertEqual(paths, sorted(paths, key=lambda p: (p.count("/"), p)))  # read shallowest first

    def test_one_of_each_kind_and_directory_before_a_second(self):
        files = [("svc/Dockerfile", 1), ("svc/Dockerfile.dev", 1), ("svc/Dockerfile.test", 1), ("api/Dockerfile", 1)]
        picked = [p for p, _ in dm.plan_files(files, max_files=2)[0]]
        self.assertEqual(picked, ["api/Dockerfile", "svc/Dockerfile"])

    def test_oversized_manifests_are_counted_not_read(self):
        picked, found, large = dm.plan_files([("package.json", dm.MAX_FILE_BYTES + 1), ("go.mod", 10),
                                              ("big.bin", dm.MAX_FILE_BYTES * 4)])
        self.assertEqual(([p for p, _ in picked], found, large), (["go.mod"], 1, ["package.json"]))


class Pagination(unittest.TestCase):
    def test_server_cap_below_per_page_keeps_paging(self):
        data = list(range(70))
        r = {f"json things?limit=50&page={p}": data[(p - 1) * 30:p * 30] for p in range(1, 5)}
        t = dm.ReplayTransport(r)
        self.assertEqual(list(dm.pages(t, "things", per_page=50, param="limit")), data)

    def test_full_pages_stop_on_a_short_one(self):
        r = {"json x?per_page=2&page=1": [1, 2], "json x?per_page=2&page=2": [3]}
        t = dm.ReplayTransport(r)
        self.assertEqual(list(dm.pages(t, "x", per_page=2)), [1, 2, 3])
        self.assertEqual(t.calls, 2)


class Extractors(unittest.TestCase):
    ctx = {"host": "gitlab.example.com", "path": "acme/app", "git": "gitlab.example.com/acme/app"}

    def test_git_ident(self):
        cases = {
            "https://github.com/Acme/API.git": "github.com/acme/api",
            "git@gitlab.example.com:acme/sub/x.git": "gitlab.example.com/acme/sub/x",
            "ssh://git@gitea.local:2222/acme/x.git": "gitea.local/acme/x",
            "git::https://gitlab.example.com/acme/tf.git//modules/vpc?ref=v1": "gitlab.example.com/acme/tf",
            "git+https://github.com/acme/lib.git@v1.2#egg=lib": "github.com/acme/lib",
            "github.com/acme/mod": "github.com/acme/mod",
            "./local": None, "/abs/path": None,
        }
        for url, want in cases.items():
            self.assertEqual(dm.git_ident(url), want, url)
        self.assertEqual(dm.git_ident("../../other/x.git", base="gitlab.example.com/acme/sub/app"),
                         "gitlab.example.com/acme/other/x")

    def test_terraform_sources(self):
        text = ('module "a" {\n  source = "acme/dns/google"\n}\n'
                'module "b" {\n  source = "app.terraform.io/acme/vpc/aws"\n}\n'
                'module "c" {\n  source = "../local"\n}\n'
                'resource "x" "y" {\n  source = "not/a/module"\n}\n')
        _, req = dm.x_terraform("main.tf", text, self.ctx)
        self.assertEqual([(r[1], r[2], r[3]) for r in req], [("tfreg", "acme/dns/google", 2), ("tfreg", "acme/vpc/aws", 5)])
        self.assertEqual(dm.tf_provides({"name": "terraform-aws-vpc", "path": "acme/terraform-aws-vpc"}),
                         [("tfreg", "acme/vpc/aws")])

    def test_gitlab_ci_remote_and_short_trigger(self):
        text = ("include:\n  - remote: 'https://gitlab.example.com/acme/tpl/-/raw/main/a.yml'\n"
                "  - template: Auto-DevOps.gitlab-ci.yml\nx:\n  trigger: acme/downstream\n")
        _, req = dm.x_gitlab_ci(".gitlab-ci.yml", text, self.ctx)
        self.assertEqual([(r[0], r[2]) for r in req], [("ci-include", "gitlab.example.com/acme/tpl"),
                                                       ("ci-trigger", "gitlab.example.com/acme/downstream")])

    def test_workflow_forms(self):
        text = ("steps:\n  - uses: ./local\n  - uses: docker://ghcr.io/acme/tool:1\n"
                "  - uses: https://gitea.local/acme/act@v1\n  - uses: acme/act2/sub@v2\n")
        _, req = dm.x_workflow("w.yml", text, dict(self.ctx, host="github.com"))
        self.assertEqual([(r[0], r[1], r[2]) for r in req], [("action", "image", "acme/tool"),
                                                             ("action", "git", "gitea.local/acme/act"),
                                                             ("action", "git", "github.com/acme/act2")])

    def test_maven_and_gradle(self):
        pom = ('<project xmlns="http://maven.apache.org/POM/4.0.0"><groupId>com.acme</groupId><artifactId>svc</artifactId>'
               '<dependencies><dependency><groupId>com.acme</groupId><artifactId>core</artifactId></dependency>'
               '</dependencies></project>')
        prov, req = dm.x_pom("pom.xml", pom, self.ctx)
        self.assertEqual(prov, [("maven", "com.acme:svc")])
        self.assertEqual([r[2] for r in req], ["com.acme:core"])
        ctx = dict(self.ctx)
        dm.x_gradle("settings.gradle", "rootProject.name = 'core'\n", ctx)
        _, req = dm.x_gradle("build.gradle", "group = 'com.acme'\ndependencies {\n  implementation 'com.acme:util:1.0'\n}\n", ctx)
        self.assertEqual([(r[2], r[3]) for r in req], [("com.acme:util", 3)])
        self.assertEqual(dm.gradle_provides(ctx, "core-repo"), [("maven", "com.acme:core")])

    def test_go_module_drops_the_port(self):
        prov, req = dm.x_go_mod("go.mod", "module localhost:3300/acme/a\nrequire localhost:3300/acme/b v1.0.0\n", self.ctx)
        self.assertEqual((prov, [r[2] for r in req]), ([("go", "localhost/acme/a")], ["localhost/acme/b"]))

    def test_python_requirements(self):
        _, req = dm.x_requirements("requirements.txt", "-r base.txt\nAcme_Client==1.0  # pinned\n"
                                   "lib @ git+https://gitlab.example.com/acme/lib.git@v2\n", self.ctx)
        self.assertEqual([(r[1], r[2], r[3]) for r in req], [("pypi", "acme-client", 2), ("pypi", "lib", 3),
                                                             ("git", "gitlab.example.com/acme/lib", 3)])

    def test_dockerfile_stage_alias_and_unresolved_arg(self):
        text = "FROM node:20 AS deps\nFROM deps\nFROM ${UNSET}\nFROM --platform=linux/amd64 registry.acme.io/acme/base:2\n"
        _, req = dm.x_dockerfile("Dockerfile", text, self.ctx)
        self.assertEqual([r[2] for r in req], ["library/node", "acme/base"])

    def test_readme_summary(self):
        self.assertEqual(dm.readme_summary("﻿# T\n\nA tool that does one useful thing well.\n"),
                         "A tool that does one useful thing well.")
        self.assertEqual(dm.readme_summary("[English](a) | [中文](b)\n\nThe actual description of it.\n"),
                         "The actual description of it.")
        self.assertEqual(dm.readme_summary("## X\n* #### A script that checks service health daily\n"),
                         "A script that checks service health daily")

    def test_example_directories_are_not_read(self):
        # found live on github.com/go-chi: chi's _examples/rest/go.mod made "chi depends on docgen"
        picked = [p for p, _ in dm.select_files([("go.mod", 10), ("_examples/rest/go.mod", 10), ("_example/go.mod", 10),
                                                 ("example/go.mod", 10), ("examples/complete/main.tf", 10),
                                                 ("cmd/example-tool/go.mod", 10)])]
        self.assertEqual(picked, ["go.mod", "cmd/example-tool/go.mod"])

    def test_readme_summary_lead_in_takes_the_table_or_list_after_it(self):
        # found live on gitlab.com: "Creates the GitLab project that backs one sandbox:" + a table
        table = ("# t\n\nCreates the GitLab project that backs one sandbox:\n\n| Resource | Purpose |\n"
                 "| --- | --- |\n| `gitlab_project` | Private repo |\n| `gitlab_branch_protection` | MR only |\n\nMore.\n")
        self.assertEqual(dm.readme_summary(table),
                         "Creates the GitLab project that backs one sandbox: gitlab_project, gitlab_branch_protection.")
        listed = "Ships two things:\n\n- the **CLI**, for people.\n- a [library](x)\n  for code\n"
        self.assertEqual(dm.readme_summary(listed), "Ships two things: the CLI, for people; a library for code.")
        self.assertEqual(dm.readme_summary("Ends with a colon but nothing after it:\n"),
                         "Ends with a colon but nothing after it:")


class CliTransportErrors(unittest.TestCase):
    """A stand-in `gh` on PATH: errors are classified and the login hint names the right command."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.environ["PATH"]

    def tearDown(self):
        os.environ["PATH"] = self.path
        shutil.rmtree(self.tmp)

    def fake(self, name, body):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        os.environ["PATH"] = self.tmp + os.pathsep + self.path

    def test_401_is_an_auth_error_with_the_login_command(self):
        self.fake("glab", 'echo "glab: 401 Unauthorized" >&2; exit 1')
        t = dm.CliTransport("gitlab", "gitlab.example.com")
        with self.assertRaises(dm.AuthError) as cm:
            t.json("groups/acme/projects")
        self.assertIn("glab auth login --hostname gitlab.example.com", str(cm.exception))

    def test_missing_scope_is_a_login_error_with_the_scopes_needed(self):
        # found live on Gitea 1.24: orgs/{org}/repos needs read:organization, which the documented
        # login lacked; gt's own hint says to create a token, ours must name the browser login
        self.fake("gt", 'echo "Error: token does not have at least one of required scope(s), required=[read:organization], '
                        'token scope=read:repository,read:user; create a token with that scope and log in with it" >&2; exit 1')
        with self.assertRaises(dm.AuthError) as cm:
            dm.CliTransport("gitea", "git.acme.io").json("orgs/acme/repos")
        self.assertIn("gt auth login --host git.acme.io --oauth --scopes read:repository,read:user,read:organization",
                      str(cm.exception))
        self.fake("glab", 'echo \'{"error":"insufficient_scope"}\'; echo "glab: HTTP 403" >&2; exit 1')
        with self.assertRaises(dm.AuthError):
            dm.CliTransport("gitlab", "gitlab.com").json("groups/acme/projects")

    def test_gitea_logged_out_is_a_login_error(self):
        # found live with gt 2.0.0 after `gt auth logout`
        self.fake("gt", 'echo "Error: no account for https://git.acme.io; run gt auth login --host https://git.acme.io, '
                        'or use -R or --host to target another instance" >&2; exit 1')
        with self.assertRaises(dm.AuthError) as cm:
            dm.CliTransport("gitea", "git.acme.io").json("orgs/acme/repos")
        self.assertIn("--oauth", str(cm.exception))

    def test_404_is_not_found(self):
        self.fake("gh", 'echo "gh: Not Found (HTTP 404)" >&2; exit 1')
        with self.assertRaises(dm.NotFound):
            dm.CliTransport("github", "github.com").json("orgs/nope/repos")

    # Rate limits: gitlab.com's free plan allows ~100 calls a minute (found live: a 7-repository
    # group took 83 calls, the next build got HTTP 429). The transport waits and retries.
    def flaky(self, name, failures, message):
        """A fake CLI that answers `message` (exit 1) for the first `failures` calls, then '[]'."""
        n = os.path.join(self.tmp, "n")
        self.fake(name, f'c=$(cat {n} 2>/dev/null || echo 0); echo $((c+1)) > {n}\n'
                        f'if [ "$c" -lt {failures} ]; then echo "{message}" >&2; exit 1; fi\necho "[]"')
        return n

    def test_429_waits_and_retries(self):
        self.flaky("glab", 2, "glab: HTTP 429")
        waits = []
        t = dm.CliTransport("gitlab", "gitlab.com", sleep=waits.append)
        self.assertEqual(t.json("groups/acme/projects"), [])
        self.assertEqual(len(waits), 2)
        self.assertEqual(t.calls, 3)

    def test_rate_limit_that_does_not_clear_is_an_api_error_naming_it(self):
        self.flaky("glab", 99, "glab: HTTP 429")
        t = dm.CliTransport("gitlab", "gitlab.com", sleep=lambda s: None)
        with self.assertRaises(dm.ApiError) as cm:
            t.json("groups/acme/projects")
        self.assertIn("rate limited by gitlab.com", str(cm.exception))
        self.assertEqual(t.calls, len(dm.RATE_WAITS) + 1)

    def test_github_secondary_rate_limit_403_is_retried(self):
        self.flaky("gh", 1, "gh: You have exceeded a secondary rate limit. (HTTP 403)")
        t = dm.CliTransport("github", "github.com", sleep=lambda s: None)
        self.assertEqual(t.json("orgs/acme/repos"), [])

    def test_other_403_is_not_retried(self):
        self.flaky("gh", 1, "gh: Resource not accessible by integration (HTTP 403)")
        t = dm.CliTransport("github", "github.com", sleep=lambda s: None)
        with self.assertRaises(dm.ApiError):
            t.json("orgs/acme/repos")
        self.assertEqual(t.calls, 1)

    def test_argv_per_provider(self):
        self.assertEqual(dm.CliTransport("github", "ghe.acme.io").argv("x", True),
                         ["gh", "api", "--hostname", "ghe.acme.io", "-H", "Accept: application/vnd.github.raw", "x"])
        self.assertEqual(dm.CliTransport("gitlab", "gitlab.com").argv("x", False), ["glab", "api", "x"])
        self.assertEqual(dm.CliTransport("gitea", "localhost:3300").argv("x", False),
                         ["gt", "--no-input", "--host", "localhost:3300", "api", "x"])


class Targets(unittest.TestCase):
    def test_parse_target(self):
        self.assertEqual(dm.parse_target("https://github.com/acme/repo"), ("github", "github.com", "acme"))
        self.assertEqual(dm.parse_target("gitlab.com/acme/platform"), ("gitlab", "gitlab.com", "acme/platform"))
        self.assertEqual(dm.parse_target("acme"), ("github", "github.com", "acme"))
        self.assertEqual(dm.parse_target("git.acme.io/acme", "gitea"), ("gitea", "git.acme.io", "acme"))
        with self.assertRaises(SystemExit):
            dm.parse_target("git.acme.io/acme")


if __name__ == "__main__":
    unittest.main(verbosity=1)
