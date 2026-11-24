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
    # ui-kit and design-tokens depend on each other (a cycle); design-tokens' CI uses ci-actions
    ("ui-kit", "design-tokens", "npm"), ("design-tokens", "ui-kit", "npm"), ("design-tokens", "ci-actions", "action"),
    # JVM: a BOM import through a parent property, a managed dependency, a version catalog, platform(),
    # an included build and a catalog plugin alias, archivesName + subprojects group, sbt and Clojure
    ("billing-core", "java-bom", "maven"), ("java-bom", "billing-core", "maven"),
    ("orders-service", "billing-core", "gradle"), ("orders-service", "java-bom", "gradle"),
    ("orders-service", "gradle-conventions", "gradle-build"), ("orders-service", "gradle-conventions", "gradle-plugin"),
    ("inventory-scala", "orders-service", "sbt"), ("inventory-scala", "billing-core", "sbt"),
    ("clj-tools", "inventory-scala", "clojure"),
    # .NET: central package management, a ProjectReference to a sibling checkout, packages.config, paket
    ("ledger-dotnet", "invoicing-dotnet", "nuget"), ("ledger-dotnet", "shared-dotnet", "dotnet-project"),
    ("ledger-dotnet", "shared-dotnet", "nuget"), ("invoicing-dotnet", "shared-dotnet", "nuget"),
    ("invoicing-dotnet", "shared-dotnet", "paket"),
    # C / C++: FetchContent, ExternalProject from a source archive, find_package against project(),
    # Conan, vcpkg and a vcpkg git registry, a Meson wrap
    ("cpp-app", "cpp-net", "cmake-fetch"), ("cpp-app", "cpp-core", "cmake-fetch"), ("cpp-app", "cpp-core", "cmake"),
    ("cpp-app", "cpp-core", "conan"), ("cpp-app", "cpp-core", "vcpkg"), ("cpp-app", "vcpkg-registry", "vcpkg"),
    ("cpp-app", "cpp-net", "meson"),
    # Rust: a git dependency and a registry crate (one edge), a path dependency to a sibling checkout
    ("rust-app", "rust-core", "cargo"), ("rust-app", "rust-shared", "cargo"),
    # Swift: SwiftPM, CocoaPods by name (podspec), Carthage
    ("swift-app", "swift-kit", "swiftpm"), ("swift-app", "swift-kit", "cocoapods"), ("swift-app", "swift-kit", "carthage"),
    # Bazel: bazel_dep + git_override (one edge), http_archive of a code-host archive
    ("bazel-app", "bazel-rules", "bazel"), ("bazel-app", "cpp-net", "bazel"),
    # Haskell, Zig, Elixir, Erlang
    ("polyglot-tools", "rust-shared", "cabal"), ("polyglot-tools", "cpp-net", "zig"),
    ("polyglot-tools", "cpp-core", "mix"), ("polyglot-tools", "bazel-rules", "rebar"),
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
    ("ledger-dotnet", "dmtest.audit"),  # NuGet prefix Dmtest. is the org's
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
        self.assertIn("36 repos (36 scanned, 0 unchanged, 0 failed, 1 incomplete)", out)
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
        self.assertIn("36 repos (1 scanned, 35 unchanged, 0 failed, 1 incomplete)", out)
        self.assertEqual(edges(dm.load_map(self.cache)), PLANTED)  # reused rows keep their edges
        rc, out = self.replay(changed, "localhost:3300/dmtest", "gitea")
        self.assertIn("(0 scanned, 36 unchanged", out)

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
        self.assertRegex(out, r"localhost:3300/dmtest\s+gitea\s+36 repos\s+43 edges out.*gaps: 1 incomplete, 4 unresolved")


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


class SeveralProviders(Tmp):
    """Found live on open-telemetry: the archived opentelemetry-js-api still declares
    @opentelemetry/api, so every closure through the API pulled in the archived repository."""

    def responses(self, old_archived=True, new=True):
        names = ["app", "lib-old"] + (["lib"] if new else [])
        r = {"json orgs/acme/repos?type=all&per_page=100&page=1":
             [gh_repo("app"), gh_repo("lib-old", archived=old_archived)] + ([gh_repo("lib")] if new else []),
             "json orgs/acme/repos?type=all&per_page=100&page=2": []}
        for n in names:
            r[f"json repos/acme/{n}/git/trees/main?recursive=1"] = {
                "tree": [{"path": "package.json", "type": "blob", "size": 50}], "truncated": False}
        r["raw repos/acme/app/contents/package.json?ref=main"] = '{"name": "app", "dependencies": {"@acme/lib": "1"}}'
        for n in names[1:]:
            r[f"raw repos/acme/{n}/contents/package.json?ref=main"] = '{"name": "@acme/lib"}'
        return r

    def test_an_archived_provider_gives_way_to_a_live_one(self):
        self.replay(self.responses(), "https://github.com/acme", "github")
        self.assertEqual(edges(dm.load_map(self.cache)), {("app", "lib", "npm")})

    def test_an_archived_sole_provider_is_kept(self):
        self.replay(self.responses(new=False), "https://github.com/acme", "github")
        self.assertEqual(edges(dm.load_map(self.cache)), {("app", "lib-old", "npm")})

    def test_two_live_providers_are_both_kept(self):
        self.replay(self.responses(old_archived=False), "https://github.com/acme", "github")
        self.assertEqual(edges(dm.load_map(self.cache)), {("app", "lib", "npm"), ("app", "lib-old", "npm")})

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
        self.assertIn("gaps: 1 repositories read incompletely, 4 internal references with no repository", out)
        m = dm.load_map(self.cache)
        self.assertEqual({(u["src"].rsplit("/", 1)[1], u["ident"]) for u in m.unresolved}, UNRESOLVED)
        self.assertEqual(edges(m), PLANTED)  # the new references and decoys add no edge
        self.assertEqual(m.internal_hints, [("crates", "dmtest-", 2), ("pypi", "dmtest-", 2)])

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
        self.assertEqual(m.internal_hints, [("crates", "dmtest-", 2)])  # only pypi was declared

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
        self.assertIn("(0 scanned, 36 unchanged", out)
        old = dm.SCANNER
        try:
            dm.SCANNER = old + 1
            rc, out = self.build()
        finally:
            dm.SCANNER = old
        self.assertIn(f"scanner {old} -> {old + 1}: rescanning every repository", out)
        self.assertIn("(36 scanned, 0 unchanged", out)

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
        self.assertIn("(36 scanned, 0 unchanged", out)

    def test_reused_repositories_keep_their_gaps(self):
        self.build()
        self.build()
        self.assertIn("file-too-large", gaps_of(dm.load_map(self.cache), "big-monorepo"))

    def test_internal_prefixes_are_stored_and_kept(self):
        rc, out = self.build("--internal", "npm:dmtest-")
        self.assertIn("5 internal references", out)  # dmtest-analytics is now the org's
        self.build()
        self.assertIn(("checkout-web", "dmtest-analytics"),
                      {(u["src"].rsplit("/", 1)[1], u["ident"]) for u in dm.load_map(self.cache).unresolved})
        rc, out = self.build("--internal", "nosuchns:acme-")
        self.assertEqual(rc, 2)
        self.assertIn("--internal takes NS:PREFIX", out)

    def test_references_into_a_group_that_is_not_built(self):
        self.build()
        m = dm.load_map(self.cache)
        self.assertEqual([(o["src"].rsplit("/", 1)[1], o["ident"], o["host"], o["group"]) for o in m.outside],
                         [("design-tokens", "localhost/brand/fonts", "localhost:3300", "brand")])
        rc, out = run("--cache", self.cache, "gaps", "design-tokens")
        self.assertIn("dmtest/design-tokens -> localhost/brand/fonts (submodule, .gitmodules:3): localhost:3300/brand "
                      "is not in the map; add it: run.sh build localhost:3300/brand --provider gitea", out)

    def test_public_hosts_are_never_outside_the_map(self):
        """github.com/google/uuid is someone else's module, not a group to build."""
        self.build()
        self.assertFalse([o for o in dm.load_map(self.cache).outside if o["ident"].startswith("github.com/")])

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
        f, hints = self.rules([("github.com/acme/a", "go", "go.acme.dev/a"), ("github.com/acme/a2", "go", "go.acme.dev/a2"),
                               ("github.com/acme/b", "maven", "com.acme:b"),
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

    def test_a_shared_vanity_domain_is_not_the_orgs(self):
        """Found live on grpc: grpc-go's google.golang.org/grpc made google.golang.org/protobuf and
        genproto look like grpc's own modules."""
        f, _ = self.rules([("github.com/grpc/grpc-go", "go", "google.golang.org/grpc")], org="grpc")
        self.assertTrue(f("go", "google.golang.org/grpc/examples"))
        self.assertFalse(f("go", "google.golang.org/protobuf"))

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
        prov, req = dm.extract({"pom.xml": pom}, dict(self.ctx))
        self.assertEqual(prov, [("maven", "com.acme:svc")])
        self.assertEqual([r[2] for r in req], ["com.acme:core"])
        prov, req = dm.extract({"settings.gradle": "rootProject.name = 'core'\n",
                                "build.gradle": "group = 'com.acme'\ndependencies {\n  implementation 'com.acme:util:1.0'\n}\n"},
                               dict(self.ctx))
        self.assertEqual([(r[2], r[4]) for r in req], [("com.acme:util", 3)])
        self.assertEqual(prov, [("maven", "com.acme:core")])

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



def reqs(files, **ctx):
    """{(via, ns, ident): (file, line, detail)} and the provides, for these files of one repository."""
    c = {"host": "github.com", "path": "acme/repo", "name": "repo", "git": "github.com/acme/repo", **ctx}
    prov, req = dm.extract(files, c)
    out = {}
    for r in req:  # the first file:line of each, as scan_repo keeps it
        out.setdefault(r[:3], r[3:])
    return out, sorted(set(prov))


class Jvm(unittest.TestCase):
    POM = '<project xmlns="http://maven.apache.org/POM/4.0.0">{}</project>'

    def test_maven_properties_come_from_the_parent_in_the_repository(self):
        parent = self.POM.format("<groupId>com.acme</groupId><artifactId>parent</artifactId><version>1</version>"
                                 "<properties><plat>com.acme.platform</plat></properties>")
        child = self.POM.format(
            "<parent><groupId>com.acme</groupId><artifactId>parent</artifactId><version>1</version></parent>"
            "<artifactId>impl</artifactId><dependencyManagement><dependencies><dependency><groupId>${plat}</groupId>"
            "<artifactId>bom</artifactId><type>pom</type><scope>import</scope></dependency></dependencies>"
            "</dependencyManagement><dependencies><dependency><groupId>${project.groupId}</groupId>"
            "<artifactId>api</artifactId></dependency><dependency><groupId>${undefined}</groupId>"
            "<artifactId>skipped</artifactId></dependency></dependencies>"
            "<build><plugins><plugin><artifactId>maven-shade-plugin</artifactId></plugin></plugins></build>")
        r, prov = reqs({"pom.xml": parent, "impl/pom.xml": child})
        self.assertIn(("maven", "com.acme:impl"), prov)
        self.assertEqual(r[("maven", "maven", "com.acme.platform:bom")][2], "BOM import")
        self.assertIn(("maven", "maven", "com.acme:api"), r)
        self.assertIn(("maven", "maven", "org.apache.maven.plugins:maven-shade-plugin"), r)
        self.assertFalse([k for k in r if "skipped" in k[2]])  # an unresolved ${property} is not guessed

    def test_registry_urls_never_keep_credentials(self):
        pom = self.POM.format("<groupId>g</groupId><artifactId>a</artifactId><repositories><repository>"
                              "<url>https://user:p4ss@repo.acme.example/maven/?token=x</url></repository></repositories>")
        r, _ = reqs({"pom.xml": pom})
        self.assertIn(("registry", "registry", "https://repo.acme.example/maven"), r)
        self.assertIsNone(dm.registry_url("${env.REPO_URL}"))

    def test_gradle_group_only_from_project_scope(self):
        """Found on opentelemetry-java: `tasks.register("x") { group = "publishing" }` before
        `subprojects { group = "io.opentelemetry" }` made every module publishing:*."""
        r, prov = reqs({"settings.gradle.kts": 'rootProject.name = "otel"\ninclude(":api:all")\n',
                        "build.gradle.kts": 'tasks.register("x") {\n  group = "publishing"\n}\n'
                                            'subprojects {\n  group = "io.opentelemetry"\n}\n',
                        "api/all/build.gradle.kts": 'base.archivesName.set("opentelemetry-api")\n'})
        self.assertIn(("maven", "io.opentelemetry:opentelemetry-api"), prov)
        self.assertFalse([p for p in prov if p[1].startswith("publishing:")])

    def test_gradle_convention_plugin_and_parent_templates(self):
        """opentelemetry-java: sdk/build.gradle.kts names its children opentelemetry-sdk-<name>, the
        convention plugin publishes them under io.opentelemetry, and a name that already has the
        prefix keeps it."""
        conv = "buildSrc/src/main/kotlin/otel.java-conventions.gradle.kts"
        r, prov = reqs({
            "settings.gradle.kts": 'include(":sdk:testing")\ninclude(":opentelemetry-shim")\n',
            "build.gradle.kts": 'subprojects {\n  group = "io.opentelemetry"\n}\n',
            "sdk/build.gradle.kts": 'subprojects {\n  group = "io.opentelemetry.sdk"\n'
                                    '  base { archivesName.set("opentelemetry-sdk-${proj.name}") }\n}\n',
            "sdk/testing/build.gradle.kts": 'plugins {\n  id("otel.java-conventions")\n}\n',
            "opentelemetry-shim/build.gradle.kts": 'plugins {\n  id("otel.java-conventions")\n}\n',
            conv: 'base {\n  archivesName.set("opentelemetry-$name")\n}\npublishing {\n  publications {\n'
                  '    register<MavenPublication>("m") {\n      groupId = "io.opentelemetry"\n    }\n  }\n}\n'})
        self.assertIn(("maven", "io.opentelemetry:opentelemetry-sdk-testing"), prov)
        self.assertIn(("maven", "io.opentelemetry:opentelemetry-shim"), prov)
        self.assertIn(("gradle-plugin", "otel.java-conventions"), prov)

    def test_gradle_build_file_named_after_the_project(self):
        """spring-framework / spring-security: settings set project.buildFileName, modules keep
        core/spring-security-core.gradle, and take the root project's group through conventions."""
        r, prov = reqs({
            "settings.gradle": "buildFiles.each { f ->\n  project.buildFileName = f.name\n}\n",
            "build.gradle": "group = 'org.springframework.security'\n",
            "core/spring-security-core.gradle": "dependencies {\n  api 'org.springframework:spring-core'\n}\n",
            "bom/spring-framework-bom.gradle": "publishing {\n  publications {\n    mavenJava(MavenPublication) {\n"
                                               "      artifactId = 'spring-bom'\n    }\n  }\n}\n",
            "gradle/publications.gradle": "apply plugin: 'maven-publish'\n"})
        self.assertIn(("maven", "org.springframework.security:spring-security-core"), prov)
        self.assertIn(("maven", "org.springframework.security:spring-bom"), prov)
        self.assertFalse([p for p in prov if p[1].endswith(":publications")])  # an applied script is not a project

    def test_gradle_projects_from_root_project_blocks_and_settings_renames(self):
        """spring-amqp / spring-kafka configure modules in `project ('spring-rabbit') { }` blocks of the
        root script; spring-ldap renames every child in settings ("spring-ldap-" + name) and one by
        findProject(...).name."""
        r, prov = reqs({"settings.gradle": 'include ":${dir.name}"\n',
                        "build.gradle": "allprojects {\n  group = 'org.springframework.amqp'\n}\n"
                                        "project ('spring-rabbit') {\n  dependencies {\n"
                                        "    api 'com.rabbitmq:amqp-client:5'\n  }\n}\n"})
        self.assertIn(("maven", "org.springframework.amqp:spring-rabbit"), prov)
        self.assertEqual(r[("gradle", "maven", "com.rabbitmq:amqp-client")][:2], ("build.gradle", 6))
        _, prov = reqs({"settings.gradle": "include 'core'\ninclude 'test-support'\n"
                                           "rootProject.children.each { p ->\n  p.name = \"spring-ldap-\" + p.name\n}\n"
                                           'findProject(":spring-ldap-test-support").name = "spring-ldap-test"\n',
                        "build.gradle": "allprojects {\n  group = 'org.springframework.ldap'\n}\n"})
        self.assertIn(("maven", "org.springframework.ldap:spring-ldap-core"), prov)
        self.assertIn(("maven", "org.springframework.ldap:spring-ldap-test"), prov)

    def test_plugin_modules_and_annotation_processors(self):
        """spring-graphql resolves a plugin through settings' useModule(...); spring-modulith's
        configuration processor is an annotationProcessorPaths <path>, not a <dependency>."""
        r, _ = reqs({"settings.gradle": "pluginManagement {\n  resolutionStrategy {\n    eachPlugin {\n"
                                        '      useModule("org.springframework.boot:spring-boot-gradle-plugin:${v}")\n'
                                        "    }\n  }\n}\n"})
        self.assertEqual(r[("gradle", "maven", "org.springframework.boot:spring-boot-gradle-plugin")][:2], ("settings.gradle", 4))
        pom = self.POM.format("<groupId>g</groupId><artifactId>a</artifactId><build><plugins><plugin>"
                              "<artifactId>maven-compiler-plugin</artifactId><configuration><annotationProcessorPaths>"
                              "<path><groupId>org.springframework.boot</groupId>"
                              "<artifactId>spring-boot-configuration-processor</artifactId></path>"
                              "</annotationProcessorPaths></configuration></plugin></plugins></build>")
        r, _ = reqs({"pom.xml": pom})
        self.assertEqual(r[("maven", "maven", "org.springframework.boot:spring-boot-configuration-processor")][2],
                         "annotation processor")

    def test_gradle_applied_scripts_count_for_the_applying_script(self):
        """temporalio/sdk-java: the group lives in gradle/versioning.gradle, applied from the root."""
        _, prov = reqs({"settings.gradle": "rootProject.name = 'sdk'\ninclude 'temporal-sdk'\n",
                        "build.gradle": 'apply from: "$rootDir/gradle/versioning.gradle"\nallprojects {\n'
                                        '  apply from: "$rootDir/gradle/publishing.gradle"\n}\n',
                        "gradle/versioning.gradle": "subprojects {\n  group = 'io.temporal'\n}\n",
                        "gradle/publishing.gradle": "def x = 1\n", "temporal-sdk/build.gradle": "\n"})
        self.assertIn(("maven", "io.temporal:temporal-sdk"), prov)
        self.assertFalse([p for p in prov if p[1].endswith((":versioning", ":publishing", ":gradle"))])

    def test_samples_are_not_read(self):
        """temporalio/sdk-java's workflowcheck samples built as if they were the SDK's own modules."""
        picked = [p for p, _ in dm.select_files([("build.gradle", 1), ("tool/samples/app/build.gradle", 1),
                                                 ("sample/build.gradle", 1), ("samplesync/build.gradle", 1)])]
        self.assertEqual(picked, ["build.gradle", "samplesync/build.gradle"])

    def test_test_source_sets_are_not_read(self):
        """spring-boot keeps 304 fixture projects under src/intTest/ and src/test/: none is its own build."""
        picked = [p for p, _ in dm.select_files([("pom.xml", 1), ("plugin/src/intTest/projects/a/pom.xml", 1),
                                                 ("plugin/src/test/resources/b/build.gradle", 1),
                                                 ("plugin/src/it/c/pom.xml", 1), ("plugin/build.gradle", 1)])]
        self.assertEqual(picked, ["pom.xml", "plugin/build.gradle"])

    def test_gradle_version_catalog(self):
        toml = ('[libraries]\nacme-core = { module = "com.acme:core", version = "1" }\n'
                'acme-util = { group = "com.acme", name = "util", version.ref = "u" }\n'
                'guava = "com.google.guava:guava:33.0"\nunused-lib = "com.acme:unused:1"\n'
                '[bundles]\nacme = ["acme-core", "acme-util"]\n'
                '[plugins]\nacme-conv = { id = "com.acme.conventions", version = "1" }\n')
        build = ('plugins {\n  alias(libs.plugins.acme.conv)\n  kotlin("jvm")\n  id("java-library")\n}\n'
                 'dependencies {\n  implementation(libs.bundles.acme)\n  implementation(libs.guava)\n'
                 '  implementation(project(":other"))\n  api(platform("com.acme:bom:1"))\n'
                 "  testImplementation group: 'com.acme', name: 'testkit', version: '1'\n}\n")
        r, _ = reqs({"gradle/libs.versions.toml": toml, "build.gradle.kts": build})
        self.assertEqual(r[("gradle", "maven", "com.acme:core")][:2], ("build.gradle.kts", 7))
        self.assertIn(("gradle", "maven", "com.acme:util"), r)
        self.assertEqual(r[("gradle-plugin", "gradle-plugin", "com.acme.conventions")][:2], ("build.gradle.kts", 2))
        self.assertTrue(r[("gradle", "maven", "com.acme:bom")][2].startswith("platform"))
        self.assertIn(("gradle", "maven", "com.acme:testkit"), r)
        # declared in the catalog but not referenced by a script that was read: still a dependency
        self.assertEqual(r[("gradle", "maven", "com.acme:unused")][:2], ("gradle/libs.versions.toml", 5))
        self.assertFalse([k for k in r if k[2] in ("java-library", "other") or "kotlin" in k[2]])

    def test_gradle_composite_builds(self):
        r, _ = reqs({"settings.gradle.kts": 'pluginManagement {\n  includeBuild("../build-logic")\n}\n'
                                            'includeBuild("tools/inner")\nsourceControl {\n'
                                            '  gitRepository(uri("https://gitlab.acme.example/libs/util.git")) {}\n}\n'})
        self.assertEqual(r[("gradle-build", "git", "github.com/acme/build-logic")][:2], ("settings.gradle.kts", 2))
        self.assertIn(("gradle-build", "git", "gitlab.acme.example/libs/util"), r)
        self.assertEqual(len(r), 2)  # a build inside the repository is not another repository

    def test_sbt(self):
        r, prov = reqs({"build.sbt": 'ThisBuild / organization := "com.acme"\nname := "inventory"\n'
                                     'libraryDependencies += "org.typelevel" %% "cats-core" % "2.10.0"\n'
                                     '.dependsOn(RootProject(uri("https://github.com/acme/core.git#v1")))\n',
                        "project/plugins.sbt": 'addSbtPlugin("com.github.sbt" % "sbt-native-packager" % "1.9")\n'})
        self.assertIn(("maven", "com.acme:inventory"), prov)
        self.assertIn(("sbt", "maven", "org.typelevel:cats-core"), r)  # no _2.13 suffix
        self.assertIn(("sbt", "git", "github.com/acme/core"), r)
        self.assertTrue(r[("sbt", "maven", "com.github.sbt:sbt-native-packager")][2].startswith("sbt plugin"))
        self.assertEqual(dm.maven_id("org.typelevel", "cats-core_2.13"), "org.typelevel:cats-core")

    def test_clojure(self):
        r, prov = reqs({"deps.edn": '{:deps {org.clojure/clojure {:mvn/version "1.11.1"}\n'
                                    ' io.github.acme/util {:git/sha "abc"}\n'
                                    ' acme/tools {:git/url "https://gitlab.com/acme/tools.git" :git/sha "1"}}}\n',
                        "sub/project.clj": '(defproject com.acme/app "1.0"\n  :dependencies [[ring "1.9.0"]])\n'})
        self.assertIn(("clojure", "maven", "org.clojure:clojure"), r)
        self.assertIn(("clojure", "git", "github.com/acme/util"), r)
        self.assertIn(("clojure", "git", "gitlab.com/acme/tools"), r)
        self.assertIn(("clojure", "maven", "ring:ring"), r)
        self.assertIn(("maven", "com.acme:app"), prov)


class DotNet(unittest.TestCase):
    def test_project_files(self):
        csproj = ('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><AssemblyName>Acme.Core</AssemblyName></PropertyGroup>'
                  '<ItemGroup><PackageReference Include="Newtonsoft.Json" Version="13.0.3" />'
                  '<PackageReference Update="Ignored.Update" />'
                  '<ProjectReference Include="..\\Inner\\Inner.csproj" />'
                  '<ProjectReference Include="..\\..\\..\\shared\\src\\S.csproj" /></ItemGroup></Project>')
        props = ('<Project><ItemGroup><PackageVersion Include="Acme.Pinned" Version="1" />'
                 '<GlobalPackageReference Include="Acme.Analyzers" Version="2" /></ItemGroup></Project>')
        r, prov = reqs({"src/Core/Core.csproj": csproj, "Directory.Packages.props": props,
                        "src/Other/Other.fsproj": "<Project />"})
        self.assertEqual(prov, [("nuget", "acme.core"), ("nuget", "other")])
        self.assertIn(("nuget", "nuget", "newtonsoft.json"), r)
        self.assertIn(("nuget", "nuget", "acme.analyzers"), r)
        self.assertNotIn(("nuget", "nuget", "acme.pinned"), r)  # a central version, not a dependency
        self.assertNotIn(("nuget", "nuget", "ignored.update"), r)
        self.assertEqual(r[("dotnet-project", "git", "github.com/acme/shared")][2],
                         "ProjectReference ../../../shared/src/S.csproj")
        self.assertEqual(len([k for k in r if k[0] == "dotnet-project"]), 1)  # ../Inner stays inside

    def test_packages_config_paket_and_sources(self):
        r, _ = reqs({"packages.config": '<packages><package id="Acme.Old" version="1" /></packages>',
                     "paket.dependencies": "// a comment\nsource https://nuget.acme.example/v3/index.json\n"
                                           "nuget Acme.New ~> 2\ngithub acme/scripts:main src/a.fs\n"
                                           "git https://gitlab.com/acme/lib.git master\n",
                     "src/paket.references": "Acme.Ref\ngroup Build\n",
                     "NuGet.Config": '<configuration><packageSources><add key="a" '
                                     'value="https://u:p@pkgs.dev.azure.com/acme/_packaging/feed/nuget/v3/index.json" />'
                                     '</packageSources></configuration>'})
        for k in (("nuget", "nuget", "acme.old"), ("nuget", "nuget", "acme.new"), ("nuget", "nuget", "acme.ref"),
                  ("paket", "git", "github.com/acme/scripts"), ("paket", "git", "gitlab.com/acme/lib"),
                  ("registry", "registry", "https://nuget.acme.example/v3/index.json"),
                  ("registry", "registry", "https://pkgs.dev.azure.com/acme/_packaging/feed/nuget/v3/index.json")):
            self.assertIn(k, r)

    def test_nuget_prefix_is_internal_only_when_it_is_the_orgs_name(self):
        m = dm.Map()
        m.graphs["github.com/acme"] = {"host": "github.com", "org": "acme"}
        m.repos["github.com/acme/a"] = {"fork": False}
        f, _ = dm.internal_rules(m, {("nuget", "acme.core"): {"github.com/acme/a"},
                                     ("nuget", "newtonsoft.json.acme"): {"github.com/acme/a"},
                                     ("maven", "com.acme:core"): {"github.com/acme/a"}})
        self.assertTrue(f("nuget", "acme.audit"))
        self.assertFalse(f("nuget", "newtonsoft.json"))
        self.assertTrue(f("gradle-plugin", "com.acme.conventions"))
        self.assertFalse(f("gradle-plugin", "org.jetbrains.kotlin.jvm"))

    def test_extractor_for_the_new_files(self):
        for path, x in (("a/b.csproj", dm.x_msbuild), ("Directory.Build.props", dm.x_msbuild),
                        ("x/packages.config", dm.x_packages_config), ("paket.dependencies", dm.x_paket),
                        ("nuget.config", dm.x_nuget_config), ("gradle/libs.versions.toml", dm.x_gradle_catalog),
                        ("buildSrc/src/main/kotlin/a.b.gradle.kts", dm.x_gradle), ("gradle.properties", dm.x_gradle),
                        ("project/Dependencies.scala", dm.x_sbt), ("build.sbt", dm.x_sbt), ("deps.edn", dm.x_deps_edn),
                        ("src/main/scala/App.scala", None), ("global.json", None)):
            self.assertIs(dm.extractor_for(path), x, path)


class Native(unittest.TestCase):
    def test_cmake(self):
        cm = ('cmake_minimum_required(VERSION 3.24)\nproject(CppApp CXX)\nset(NET_URL "https://gitlab.acme.example/cpp/net.git")\n'
              'include(FetchContent)\nFetchContent_Declare(\n  net\n  GIT_REPOSITORY ${NET_URL}\n  GIT_TAG v1.2\n)\n'
              'FetchContent_Declare(googletest URL https://github.com/google/googletest/archive/refs/tags/v1.14.0.zip)\n'
              'ExternalProject_Add(legacy URL https://gitlab.acme.example/cpp/legacy/-/archive/v2/legacy-v2.tar.gz)\n'
              'CPMAddPackage("gh:fmtlib/fmt@10.2.1")\nCPMAddPackage(NAME util GITHUB_REPOSITORY acme/util VERSION 1.0)\n'
              '# FetchContent_Declare(old GIT_REPOSITORY https://github.com/acme/commented-out.git)\n'
              'find_package(AcmeCore 2.0 REQUIRED)\nfind_package(Threads)\ninstall(EXPORT AcmeAppTargets DESTINATION lib)\n')
        r, prov = reqs({"CMakeLists.txt": cm, "cmake/AcmeNetConfig.cmake.in": "@PACKAGE_INIT@\n"})
        self.assertEqual(r[("cmake-fetch", "git", "gitlab.acme.example/cpp/net")][:2], ("CMakeLists.txt", 5))
        for k in (("cmake-fetch", "git", "github.com/google/googletest"), ("cmake-fetch", "git", "gitlab.acme.example/cpp/legacy"),
                  ("cmake-fetch", "git", "github.com/fmtlib/fmt"), ("cmake-fetch", "git", "github.com/acme/util"),
                  ("cmake", "cmake", "acmecore"), ("cmake", "cmake", "threads")):
            self.assertIn(k, r)
        self.assertNotIn(("cmake-fetch", "git", "github.com/acme/commented-out"), r)
        self.assertEqual(prov, [("cmake", "acmeapp"), ("cmake", "acmenet"), ("cmake", "cppapp")])

    def test_conan_vcpkg_meson(self):
        r, prov = reqs({
            "conanfile.txt": "[requires]\nacme-core/1.0@acme/stable\nzlib/1.3\n[generators]\nCMakeDeps\n",
            "pkg/conanfile.py": 'class P(ConanFile):\n    name = "acme-net"\n    requires = "acme-core/1.0", "boost/1.84"\n'
                                '    def requirements(self):\n        self.requires("openssl/3.2")\n',
            "vcpkg.json": '{"name": "acme-app", "dependencies": ["fmt", {"name": "acme-core", "features": ["x"]}]}',
            "vcpkg-configuration.json": '{"registries": [{"kind": "git", "repository": "https://gitlab.acme.example/cpp/vcpkg-registry.git",'
                                        ' "packages": ["acme-core"]}]}',
            "subprojects/net.wrap": "[wrap-git]\nurl = https://gitlab.acme.example/cpp/net.git\nrevision = v1\n",
            "subprojects/zlib.wrap": "[wrap-file]\nsource_url = https://github.com/madler/zlib/archive/v1.3.tar.gz\n"})
        for k in (("conan", "conan", "acme-core"), ("conan", "conan", "zlib"), ("conan", "conan", "boost"),
                  ("conan", "conan", "openssl"), ("vcpkg", "vcpkg", "fmt"), ("vcpkg", "vcpkg", "acme-core"),
                  ("vcpkg", "git", "gitlab.acme.example/cpp/vcpkg-registry"), ("meson", "git", "gitlab.acme.example/cpp/net"),
                  ("meson", "git", "github.com/madler/zlib")):
            self.assertIn(k, r)
        self.assertNotIn(("conan", "conan", "cmakedeps"), r)
        self.assertEqual(prov, [("conan", "acme-net"), ("vcpkg", "acme-app")])

    def test_cargo(self):
        cargo = ('[package]\nname = "acme_app"\n\n[dependencies]\nserde = { version = "1", features = ["derive"] }\n'
                 'acme-types = { git = "https://gitlab.acme.example/rust/core.git", branch = "main" }\n'
                 'acme-macros = { version = "0.2", registry = "acme" }\nshared = { path = "../../rust-shared" }\n'
                 'local = { path = "crates/local" }\nrenamed = { package = "acme_util", version = "1" }\n\n'
                 "[target.'cfg(unix)'.dependencies]\nlibc = \"0.2\"\n\n[dependencies.tokio]\nversion = \"1\"\nfeatures = [\n  \"full\",\n]\n\n"
                 '[patch.crates-io]\nacme-types = { git = "https://gitlab.acme.example/rust/core-fork.git" }\n')
        r, prov = reqs({"app/Cargo.toml": cargo,
                        ".cargo/config.toml": '[registries.acme]\nindex = "sparse+https://u:p@crates.acme.example/index/"\n'})
        self.assertEqual(prov, [("crates", "acme-app")])
        self.assertEqual(r[("cargo", "git", "gitlab.acme.example/rust/core")][:2], ("app/Cargo.toml", 6))
        for k in (("cargo", "crates", "serde"), ("cargo", "crates", "acme-macros"), ("cargo", "git", "github.com/acme/rust-shared"),
                  ("cargo", "crates", "acme-util"), ("cargo", "crates", "libc"), ("cargo", "crates", "tokio"),
                  ("cargo", "git", "gitlab.acme.example/rust/core-fork"),
                  ("registry", "registry", "https://crates.acme.example/index")):
            self.assertIn(k, r)
        self.assertEqual(r[("cargo", "crates", "tokio")][:2], ("app/Cargo.toml", 16))
        self.assertFalse([k for k in r if k[2] in ("local", "acme-types", "shared")])  # path / git deps are not crates

    def test_swift_pods_carthage(self):
        r, prov = reqs({"Package.swift": 'let package = Package(\n  name: "App",\n  dependencies: [\n'
                                         '    .package(url: "https://gitlab.acme.example/ios/kit.git", from: "1.0.0"),\n'
                                         '    .package(url: "https://github.com/apple/swift-log.git", from: "1.5.0"),\n'
                                         '    .package(path: "../ios-shared"),\n  ]\n)\n',
                        "Podfile": "target 'App' do\n  pod 'AcmeKit'\n  pod 'Net', :git => 'https://gitlab.acme.example/ios/net.git'\n"
                                   "  pod 'Alamofire', '~> 5.8'\nend\n",
                        "AcmeKit.podspec": "Pod::Spec.new do |s|\n  s.name = 'AcmeKit'\nend\n",
                        "Cartfile": 'github "Alamofire/Alamofire" ~> 5.8\ngit "https://gitlab.acme.example/ios/legacy.git" "main"\n'})
        for k in (("swiftpm", "git", "gitlab.acme.example/ios/kit"), ("swiftpm", "git", "github.com/apple/swift-log"),
                  ("swiftpm", "git", "github.com/acme/ios-shared"), ("cocoapods", "cocoapods", "acmekit"),
                  ("cocoapods", "git", "gitlab.acme.example/ios/net"), ("cocoapods", "cocoapods", "alamofire"),
                  ("carthage", "git", "github.com/alamofire/alamofire"), ("carthage", "git", "gitlab.acme.example/ios/legacy")):
            self.assertIn(k, r)
        self.assertEqual(r[("swiftpm", "git", "gitlab.acme.example/ios/kit")][:2], ("Package.swift", 4))
        self.assertEqual(prov, [("cocoapods", "acmekit")])

    def test_bazel(self):
        r, prov = reqs({
            "MODULE.bazel": 'module(name = "acme_app", version = "1.0")\nbazel_dep(name = "rules_cc", version = "0.0.9")\n'
                            'bazel_dep(name = "acme_rules", version = "1.2")\ngit_override(\n    module_name = "acme_rules",\n'
                            '    remote = "https://gitlab.acme.example/build/rules.git",\n    commit = "abc",\n)\n',
            "WORKSPACE": 'workspace(name = "acme_app")\nhttp_archive(\n    name = "acme_net",\n'
                         '    urls = ["https://gitlab.acme.example/cpp/net/-/archive/v1/net-v1.tar.gz"],\n)\n'
                         'http_archive(name = "zlib", urls = ["https://zlib.net/zlib-1.3.tar.gz"])\n'
                         'git_repository(name = "abseil", remote = "https://github.com/abseil/abseil-cpp.git")\n',
            "bazel/repositories.bzl": 'def deps():\n    new_git_repository(name = "x", remote = "https://gitlab.acme.example/cpp/x.git")\n'})
        for k in (("bazel", "bazel", "rules_cc"), ("bazel", "bazel", "acme_rules"), ("bazel", "git", "gitlab.acme.example/build/rules"),
                  ("bazel", "git", "gitlab.acme.example/cpp/net"), ("bazel", "git", "github.com/abseil/abseil-cpp"),
                  ("bazel", "git", "gitlab.acme.example/cpp/x")):
            self.assertIn(k, r)
        self.assertEqual(r[("bazel", "git", "gitlab.acme.example/build/rules")][:2], ("MODULE.bazel", 4))
        self.assertEqual(len([k for k in r if k[0] == "bazel"]), 6)  # zlib.net is not a code host
        self.assertEqual(prov, [("bazel", "acme_app")])

    def test_cabal_zig_mix_rebar(self):
        r, _ = reqs({"cabal.project": "packages: .\nsource-repository-package\n  type: git\n  location: https://gitlab.acme.example/hs/lib.git\n",
                     "build.zig.zon": '.{ .dependencies = .{ .net = .{ .url = "git+https://gitlab.acme.example/zig/net.git#abc" } } }\n',
                     "mix.exs": 'defp deps do\n  [{:plug, "~> 1.0"}, {:core, git: "https://gitlab.acme.example/ex/core.git", tag: "v1"},\n'
                                '   {:tool, github: "acme/tool"}]\nend\n',
                     "rebar.config": '{deps, [{lib, {git, "https://gitlab.acme.example/erl/lib.git", {tag, "1"}}}]}.\n'})
        for k in (("cabal", "git", "gitlab.acme.example/hs/lib"), ("zig", "git", "gitlab.acme.example/zig/net"),
                  ("mix", "git", "gitlab.acme.example/ex/core"), ("mix", "git", "github.com/acme/tool"),
                  ("rebar", "git", "gitlab.acme.example/erl/lib")):
            self.assertIn(k, r)
        self.assertEqual(len(r), 5)

    def test_line_numbers_from_grpc(self):
        """Found live on grpc: a multi-line .package( cited the line without the URL, and ^\\s* in
        multiline mode cited the blank line above a pod."""
        r, _ = reqs({"Package.swift": 'let package = Package(\n  dependencies: [\n    .package(\n'
                                      '      url: "https://github.com/grpc/grpc-swift-2.git",\n      from: "2.0.0"\n    ),\n  ]\n)\n',
                     "Podfile": "target 'A' do\n\n\n  pod 'BoringSSL-GRPC', :path => '../..'\nend\n"})
        self.assertEqual(r[("swiftpm", "git", "github.com/grpc/grpc-swift-2")][:2], ("Package.swift", 4))
        self.assertEqual(r[("cocoapods", "cocoapods", "boringssl-grpc")][:2], ("Podfile", 4))

    def test_a_bazel_registry_entry_is_not_the_repositorys_module(self):
        r, prov = reqs({"modules/abseil-cpp/20240116.0/MODULE.bazel": 'module(name = "abseil-cpp")\nbazel_dep(name = "rules_cc")\n',
                        "MODULE.bazel": 'module(name = "registry_tools")\n'})
        self.assertEqual((r, prov), ({}, [("bazel", "registry_tools")]))

    def test_gradle_project_dir_keeps_the_project_name(self):
        """grpc-java: project(':grpc-alts').projectDir = "$rootDir/alts" as File."""
        _, prov = reqs({"settings.gradle": 'include ":grpc-alts"\nproject(\':grpc-alts\').projectDir = "$rootDir/alts" as File\n',
                        "build.gradle": "subprojects {\n  group = 'io.grpc'\n}\n", "alts/build.gradle": "\n"})
        self.assertIn(("maven", "io.grpc:grpc-alts"), prov)
        self.assertNotIn(("maven", "io.grpc:alts"), prov)

    def test_archive_urls(self):
        for url, want in (("https://github.com/o/r/archive/refs/tags/v1.tar.gz", "github.com/o/r"),
                          ("https://github.com/o/r/releases/download/v1/r.tar.gz", "github.com/o/r"),
                          ("https://codeload.github.com/o/r/tar.gz/v1", "github.com/o/r"),
                          ("https://gitlab.acme.example/a/b/c/-/archive/v1/c.tar.gz", "gitlab.acme.example/a/b/c"),
                          ("https://zlib.net/zlib-1.3.tar.gz", None)):
            self.assertEqual(dm.archive_repo(url), want, url)

    def test_crates_names_share_the_org_name_hint(self):
        m = dm.Map()
        m.graphs["github.com/acme"] = {"host": "github.com", "org": "acme"}
        m.repos["github.com/acme/a"] = {"fork": False}
        f, hints = dm.internal_rules(m, {("crates", "acme-core"): {"github.com/acme/a"}, ("crates", "acme-net"): {"github.com/acme/a"}})
        self.assertEqual(hints, [("crates", "acme-", 2)])
        self.assertFalse(f("crates", "acme-other"))  # crates.io has no namespaces: only with --internal

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
