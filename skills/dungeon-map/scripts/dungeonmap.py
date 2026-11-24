#!/usr/bin/env python3
"""dungeonmap.py -- a map of every repository in an org (stdlib only, Python 3.10+).

  dungeonmap.py build https://github.com/acme              # one graph per org / GitLab group
  dungeonmap.py build gitea.example.com/acme --provider gitea
  dungeonmap.py maps                                       # every graph in the local cache

Reads repository metadata and a few manifest files through the provider's own CLI (`gh api`,
`glab api`, `gt api`), so the user's existing login is used and this script never sees a token.
Nothing is cloned. Each org (and each GitLab subgroup) becomes one SQLite graph under
~/.cache/dungeon-map/<host>/<path>/map.db (DUNGEONMAP_DIR); together they are the top-level map.

Per repository the graph keeps metadata (description, topics, language, default branch, archived,
last activity, README summary), what it *provides* (its git URL, go module, npm / PyPI / Maven /
Helm names, Terraform registry name, container image path) and what it *requires* (submodules,
package dependencies, Helm dependencies, Terraform module sources, GitLab CI include / trigger /
component, GitHub workflow and action `uses:`, Dockerfile FROM), each with file:line. Requires are
resolved against the provides of every cached graph when the map is loaded (load_map), so an
edge between two orgs appears as soon as both are built.

compass (../../compass) imports this module to query the map.
"""
import argparse
import concurrent.futures
import configparser
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET

try:  # Python 3.11+; 3.10 falls back to a line-based reader for pyproject.toml
    import tomllib
except ImportError:  # pragma: no cover - exercised only on 3.10
    tomllib = None

SCHEMA = 1
# What the extractors and select_files read. Bump it whenever either changes: the next build then
# rescans every repository, so the fix reaches repositories that have not changed since.
SCANNER = 4
# Manifests read per repository. 300 reads all but 3 of open-telemetry's 113 repositories completely
# (4,032 file reads for the org; reading all of them would take ~4,600, close to GitHub's 5,000 calls
# an hour). What the cap leaves out is reported as a gap, never dropped silently.
MAX_FILES = 300
# Tree entries listed per repository on GitLab (100 a page) and Gitea (the server's page size).
MAX_TREE_ENTRIES = 20000
# The environment each edge is needed in (compass deps --kinds). Every via an extractor emits is
# listed (a test checks it); later ecosystems add theirs here.
EDGE_KINDS = ("build", "ci", "dev", "deploy", "runtime")
VIA_KIND = {
    "submodule": "build", "npm": "build", "go": "build", "python": "build", "maven": "build",
    "gradle": "build", "terraform": "build", "image": "build",
    "gradle-plugin": "build", "gradle-build": "build", "sbt": "build", "clojure": "build",
    "nuget": "build", "dotnet-project": "build", "paket": "build",
    "cmake": "build", "cmake-fetch": "build", "conan": "build", "vcpkg": "build", "meson": "build",
    "cargo": "build", "swiftpm": "build", "cocoapods": "build", "carthage": "build", "bazel": "build",
    "cabal": "build", "zig": "build", "mix": "build", "rebar": "build",
    # a package registry URL (repositories, nuget.config): recorded for dm-registries-static,
    # never matched to a repository yet
    "registry": "build",
    "ci-include": "ci", "ci-component": "ci", "ci-trigger": "ci", "workflow": "ci", "action": "ci",
    "helm": "deploy",
}
# The ecosystem of each via. compass deps follows a repository it reached through one ecosystem only
# through that ecosystem's edges and language-neutral ones (None: submodules, images, Terraform,
# Helm, CI, Bazel), so a Java build that uses a polyglot monorepo's Java part does not pull in
# everything its Go / Python / JS parts depend on.
VIA_FAMILY = {
    "maven": "jvm", "gradle": "jvm", "gradle-plugin": "jvm", "gradle-build": "jvm", "sbt": "jvm", "clojure": "jvm",
    "go": "go", "python": "python", "npm": "js",
    "nuget": "dotnet", "dotnet-project": "dotnet", "paket": "dotnet",
    "cargo": "rust", "swiftpm": "swift", "cocoapods": "swift", "carthage": "swift",
    "cmake": "native", "cmake-fetch": "native", "conan": "native", "vcpkg": "native", "meson": "native",
    "cabal": "haskell", "zig": "zig", "mix": "beam", "rebar": "beam",
}
# Code hosts shared by many owners: a Go module path on one is internal only under host/org/.
SHARED_GO_HOSTS = {"github.com", "gitlab.com", "bitbucket.org", "gitea.com", "codeberg.org", "gopkg.in"}
CACHE = os.environ.get("DUNGEONMAP_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "dungeon-map")
PROVIDERS = ("github", "gitlab", "gitea")
KNOWN_HOSTS = {"github.com": "github", "gitlab.com": "gitlab", "gitea.com": "gitea"}
LOGIN_HINT = {
    "github": "gh auth login{host}",
    "gitlab": "glab auth login{host}",
    "gitea": "gt auth login --host {h} --oauth --scopes read:repository,read:user,read:organization",
}
MAX_FILE_BYTES = 512 * 1024
# Waits (seconds) after each rate-limited answer before trying again; then the call fails. gitlab.com's
# free plan allows ~100 calls a minute per user (a repository costs ~12), GitHub's secondary limits ask
# for a minute or more. Matched on the CLI's message: HTTP 429, or a 403 that says "rate limit".
RATE_WAITS = (15, 30, 60, 60, 60)
RATE_LIMITED = re.compile(r"\b429\b|rate limit|too many requests")


class ApiError(Exception):
    pass


class NotFound(ApiError):
    pass


class AuthError(ApiError):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------------- transport
class CliTransport:
    """Runs the provider CLI's `api` command. The CLI holds the login; we only read stdout."""

    def __init__(self, provider, host, timeout=60, sleep=time.sleep):
        self.provider, self.host, self.timeout = provider, host, timeout
        self.calls = 0
        self._sleep = sleep
        self._lock = threading.Lock()
        self._resume = 0.0  # time.monotonic() before which no thread calls (one of them hit a rate limit)

    def _hold(self):
        """Wait out a rate-limit pause another thread started, so the pool does not burn calls on 429s."""
        delay = self._resume - time.monotonic()
        if delay > 0:
            self._sleep(delay)

    def _pause(self, wait):
        with self._lock:
            now = time.monotonic()
            if now >= self._resume:  # a new pause, not one already in effect: say so once
                print(f"dungeon-map: rate limited by {self.host}; waiting {wait}s, then continuing", file=sys.stderr)
            self._resume = max(self._resume, now + wait)
        self._hold()

    def argv(self, endpoint, raw):
        p, h = self.provider, self.host
        if p == "github":
            a = ["gh", "api"] + ([] if h == "github.com" else ["--hostname", h])
            if raw:
                a += ["-H", "Accept: application/vnd.github.raw"]
        elif p == "gitlab":
            a = ["glab", "api"] + ([] if h == "gitlab.com" else ["--hostname", h])
        else:
            a = ["gt", "--no-input", "--host", h, "api"]
        return a + [endpoint]

    def fetch(self, endpoint, raw=False):
        exe = self.argv(endpoint, raw)
        if not shutil.which(exe[0]):
            raise ApiError(f"{exe[0]} is not installed (needed for {self.provider}); install it and log in: "
                           + login_hint(self.provider, self.host))
        env = dict(os.environ, GH_PROMPT_DISABLED="1", NO_COLOR="1", GLAB_NO_PROMPT="1")
        self._hold()  # before a retry, _pause has already waited
        for wait in (*RATE_WAITS, None):
            with self._lock:
                self.calls += 1
            try:
                r = subprocess.run(exe, capture_output=True, text=True, timeout=self.timeout, env=env)
            except subprocess.TimeoutExpired:
                raise ApiError(f"{exe[0]} api {endpoint}: timed out after {self.timeout}s") from None
            if r.returncode == 0:
                return r.stdout
            low = (r.stderr + r.stdout).lower()
            if not RATE_LIMITED.search(low):
                break
            if wait is None:
                raise ApiError(f"{exe[0]} api {endpoint}: rate limited by {self.host} for {sum(RATE_WAITS)}s; "
                               "run the build again in a few minutes")
            self._pause(wait)
        msg = (r.stderr.strip() or r.stdout.strip()).splitlines()
        text = " ".join(msg[-3:])[:400]
        if "404" in low or "not found" in low:
            raise NotFound(endpoint)
        # a login without a permission the call needs (Gitea: "required scope(s)", GitLab: insufficient_scope)
        if "required scope" in low or "insufficient_scope" in low:
            raise AuthError(f"{exe[0]}'s login to {self.host} lacks a permission this needs ({text}); "
                            f"log in again: {login_hint(self.provider, self.host)}")
        if any(k in low for k in ("401", "unauthorized", "not logged in", "no account for", "authentication", "bad credentials", "no token")):
            raise AuthError(f"{exe[0]} is not logged in to {self.host} ({text}); run: {login_hint(self.provider, self.host)}")
        raise ApiError(f"{exe[0]} api {endpoint}: {text}")

    def json(self, endpoint):
        out = self.fetch(endpoint)
        try:
            return json.loads(out) if out.strip() else None
        except json.JSONDecodeError:
            raise ApiError(f"{endpoint}: response is not JSON: {out[:120]!r}") from None

    def raw(self, endpoint):
        return self.fetch(endpoint, raw=True)


class RecordingTransport:
    """Wraps a transport and keeps every response, for --save (test fixtures, offline sharing)."""

    def __init__(self, inner):
        self.inner, self.saved = inner, {}

    @property
    def calls(self):
        return self.inner.calls

    def _do(self, kind, endpoint):
        key = f"{kind} {endpoint}"
        try:
            body = getattr(self.inner, kind)(endpoint)
        except NotFound:
            self.saved[key] = None
            raise
        self.saved[key] = body
        return body

    def json(self, endpoint):
        return self._do("json", endpoint)

    def raw(self, endpoint):
        return self._do("raw", endpoint)


class ReplayTransport:
    """Answers from a responses.json written by --save (or built by a test). Missing = 404."""

    def __init__(self, responses):
        self.responses, self.calls = responses, 0

    def _do(self, kind, endpoint):
        self.calls += 1
        body = self.responses.get(f"{kind} {endpoint}")
        if body is None:
            raise NotFound(endpoint)
        return body

    def json(self, endpoint):
        return self._do("json", endpoint)

    def raw(self, endpoint):
        return self._do("raw", endpoint)


def login_hint(provider, host):
    known = KNOWN_HOSTS.get(host) == provider
    return LOGIN_HINT[provider].format(host="" if known else f" --hostname {host}", h=host)


def pages(t, endpoint, per_page=100, param="per_page", max_pages=100):
    """Yield every item of a paged list. Stops on an empty page, or on a short page once a full
    page proved the server honours per_page (a server cap below per_page then still pages on)."""
    sep = "&" if "?" in endpoint else "?"
    full_seen = False
    for page in range(1, max_pages + 1):
        data = t.json(f"{endpoint}{sep}{param}={per_page}&page={page}") or []
        if not data:
            return
        yield from data
        if len(data) >= per_page:
            full_seen = True
        elif full_seen:
            return


def q(s):
    return urllib.parse.quote(s, safe="")


# --------------------------------------------------------------------------------- providers
class GitHub:
    name = "github"

    def __init__(self, t, host):
        self.t, self.host = t, host

    def list_repos(self, org):
        try:
            items = list(pages(self.t, f"orgs/{q(org)}/repos?type=all"))
        except NotFound:  # a user account, not an org
            items = list(pages(self.t, f"users/{q(org)}/repos?type=owner"))
        return [{
            "path": r["full_name"], "name": r["name"], "description": r.get("description") or "",
            "topics": r.get("topics") or [], "language": r.get("language") or "",
            "default_branch": r.get("default_branch") or "", "archived": bool(r.get("archived")),
            "fork": bool(r.get("fork")), "last_activity": r.get("pushed_at") or r.get("updated_at") or "",
            "web_url": r.get("html_url") or "", "clone_https": r.get("clone_url") or "",
            "clone_ssh": r.get("ssh_url") or "", "empty": (r.get("size") == 0), "_id": r["full_name"],
        } for r in items]

    def subgroups(self, org):
        return []

    def tree(self, repo, max_entries=MAX_TREE_ENTRIES):
        """([(path, size)], why the listing is incomplete or ""). One call; GitHub cuts it itself."""
        d = self.t.json(f"repos/{repo['path']}/git/trees/{q(repo['default_branch'])}?recursive=1") or {}
        files = [(e["path"], e.get("size")) for e in d.get("tree", []) if e.get("type") == "blob"]
        cut = ("GitHub truncated the tree listing (over 100,000 entries or 7 MB): "
               f"{len(d.get('tree', []))} entries listed") if d.get("truncated") else ""
        return files, cut

    def read(self, repo, path):
        return self.t.raw(f"repos/{repo['path']}/contents/{urllib.parse.quote(path)}?ref={q(repo['default_branch'])}")


class GitLab:
    name = "gitlab"

    def __init__(self, t, host):
        self.t, self.host = t, host

    def list_repos(self, group):
        try:
            items = list(pages(self.t, f"groups/{q(group)}/projects?include_subgroups=false&with_shared=false"))
        except NotFound:  # a user namespace
            items = list(pages(self.t, f"users/{q(group)}/projects"))
        out = []
        for r in items:
            out.append({
                "path": r["path_with_namespace"], "name": r["path"], "description": r.get("description") or "",
                "topics": r.get("topics") or r.get("tag_list") or [], "language": "",
                "default_branch": r.get("default_branch") or "", "archived": bool(r.get("archived")),
                "fork": bool(r.get("forked_from_project")), "last_activity": r.get("last_activity_at") or "",
                "web_url": r.get("web_url") or "", "clone_https": r.get("http_url_to_repo") or "",
                "clone_ssh": r.get("ssh_url_to_repo") or "", "empty": bool(r.get("empty_repo")), "_id": str(r["id"]),
            })
        return out

    def subgroups(self, group):
        # all_available: without it GitLab lists only the subgroups the user is a member of, so a
        # developer without direct membership saw none (found on gitlab.com/inkscape: 0 of 7)
        try:
            return [g["full_path"] for g in pages(self.t, f"groups/{q(group)}/subgroups?all_available=true")]
        except NotFound:
            return []

    def language(self, repo):
        try:
            langs = self.t.json(f"projects/{repo['_id']}/languages") or {}
        except NotFound:
            return ""
        return max(langs, key=langs.get) if langs else ""

    def tree(self, repo, max_entries=MAX_TREE_ENTRIES):
        """([(path, None)], why the listing is incomplete or ""): 100 entries a call, up to max_entries."""
        files, n, max_pages = [], 0, max(1, -(-max_entries // 100))
        for e in pages(self.t, f"projects/{repo['_id']}/repository/tree?recursive=true&ref={q(repo['default_branch'])}",
                       max_pages=max_pages):
            n += 1
            if e.get("type") == "blob":
                files.append((e["path"], None))
        cut = f"tree listing stopped at {n} entries (--max-tree-entries {max_entries})" if n >= 100 * max_pages else ""
        return files, cut

    def read(self, repo, path):
        return self.t.raw(f"projects/{repo['_id']}/repository/files/{q(path)}/raw?ref={q(repo['default_branch'])}")


class Gitea:
    name = "gitea"

    def __init__(self, t, host):
        self.t, self.host = t, host

    def list_repos(self, org):
        try:
            items = list(pages(self.t, f"orgs/{q(org)}/repos", per_page=50, param="limit"))
        except NotFound:  # a user account
            items = list(pages(self.t, f"users/{q(org)}/repos", per_page=50, param="limit"))
        out = []
        for r in items:
            out.append({
                "path": r["full_name"], "name": r["name"], "description": r.get("description") or "",
                "topics": r.get("topics"), "language": r.get("language") or "",
                "default_branch": r.get("default_branch") or "", "archived": bool(r.get("archived")),
                "fork": bool(r.get("fork")), "last_activity": r.get("updated_at") or "",
                "web_url": r.get("html_url") or "", "clone_https": r.get("clone_url") or "",
                "clone_ssh": r.get("ssh_url") or "", "empty": bool(r.get("empty")), "_id": r["full_name"],
            })
        return out

    def subgroups(self, org):
        return []

    def topics(self, repo):  # older Gitea leaves topics out of the repository listing
        try:
            return (self.t.json(f"repos/{repo['path']}/topics") or {}).get("topics") or []
        except NotFound:
            return []

    def tree(self, repo, max_entries=MAX_TREE_ENTRIES):
        """([(path, size)], why the listing is incomplete or ""): pages of the server's size (up to
        1000, DEFAULT_GIT_TREES_PER_PAGE) until total_count is listed or max_entries is reached."""
        files, seen, page = [], 0, 1
        while True:
            d = self.t.json(f"repos/{repo['path']}/git/trees/{q(repo['default_branch'])}"
                            f"?recursive=true&per_page=1000&page={page}") or {}
            ents = d.get("tree") or []
            seen += len(ents)
            files += [(e["path"], e.get("size")) for e in ents if e.get("type") == "blob"]
            total = d.get("total_count") or 0
            if not ents or not d.get("truncated") and seen >= total:
                return files, ""
            if seen >= max_entries:
                return files, f"tree listing stopped at {seen} of {total} entries (--max-tree-entries {max_entries})"
            page += 1

    def read(self, repo, path):
        return self.t.raw(f"repos/{repo['path']}/raw/{urllib.parse.quote(path)}?ref={q(repo['default_branch'])}")


PROVIDER_CLASS = {"github": GitHub, "gitlab": GitLab, "gitea": Gitea}


# --------------------------------------------------------------------------------- identities
def git_ident(url, base=None):
    """Normalize a git URL (https, ssh, scp-like, terraform git::, pip git+, host/path shorthand)
    to 'host/path' (lowercase, no .git). `base` ('host/path' of the referring repo) resolves
    relative submodule URLs. Returns None for anything that is not a remote repository."""
    u = (url or "").strip().strip("'\"")
    if not u or u.startswith(("./", "/")) and not u.startswith("//"):
        return None
    if u.startswith("../") and base:
        parts = base.split("/")
        rel = u
        while rel.startswith("../"):
            rel, parts = rel[3:], parts[:-1]
        return norm_path("/".join(parts + [rel]))
    u = re.sub(r"^(git::|git\+|hg::)", "", u)
    u = u.split("?", 1)[0].split("#", 1)[0]
    m = re.match(r"^[a-z][a-z0-9+.-]*://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", u, re.I)
    if m:
        host, path = m.group(1), m.group(2)
    else:
        m = re.match(r"^(?:[^@/]+@)?([^/:]+\.[^/:]+):(?!\d+/)(.+)$", u)  # git@host:group/repo.git
        if m:
            host, path = m.group(1), m.group(2)
        else:
            m = re.match(r"^([a-z0-9-]+(?:\.[a-z0-9-]+)+)/(.+)$", u, re.I)  # github.com/o/r shorthand
            if not m:
                return None
            host, path = m.group(1), m.group(2)
    path = path.split("//", 1)[0]  # terraform subdirectory
    path = re.sub(r"@[^/]*$", "", path)  # pip ...repo.git@v1
    return norm_path(f"{host}/{path}")


def norm_path(p):
    p = p.strip("/").lower()
    p = re.sub(r"\.git$", "", p)
    p = re.sub(r"/+", "/", p)
    return p if "/" in p else None


def pypi_norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def line_of(text, needle, start=0):
    i = text.find(needle, start)
    return text.count("\n", 0, i) + 1 if i >= 0 else 0


# --------------------------------------------------------------------------------- extractors
# Each extractor takes (path, text, ctx) and returns (provides, requires):
#   provides: [(ns, ident)]          requires: [(via, ns, ident, line, detail)]
# ctx: {"host": repo host, "path": repo path, "git": 'host/path' of the repo, "provider": ...}

def x_gitmodules(path, text, ctx):
    req = []
    for i, line in enumerate(text.splitlines(), 1):
        m = re.match(r"\s*url\s*=\s*(\S+)", line)
        if m:
            g = git_ident(m.group(1), base=ctx["git"])
            if g:
                req.append(("submodule", "git", g, i, m.group(1)))
    return [], req


def x_package_json(path, text, ctx):
    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        return [], []
    prov, req = [], []
    if isinstance(d.get("name"), str):
        prov.append(("npm", d["name"].lower()))
    for sect in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        for name, ver in (d.get(sect) or {}).items():
            ln = line_of(text, f'"{name}"')
            req.append(("npm", "npm", name.lower(), ln, f"{sect} {ver}"))
            v = str(ver)
            g = None
            if re.match(r"^(git\+|git://|https?://|ssh://)", v):
                g = git_ident(v)
            elif re.match(r"^(github|gitlab):", v):
                h = "github.com" if v.startswith("github:") else "gitlab.com"
                g = norm_path(f"{h}/{v.split(':', 1)[1].split('#')[0]}")
            if g:
                req.append(("npm", "git", g, ln, v))
    return prov, req


def go_ident(mod):
    """Module path, lowercased, without a port on its host (git identities never carry one)."""
    return re.sub(r"^([^/:]+):\d+/", r"\1/", mod.strip('"').lower())


def x_go_mod(path, text, ctx):
    prov, req, block = [], [], False
    for i, line in enumerate(text.splitlines(), 1):
        s = line.split("//", 1)[0].strip()
        m = re.match(r"module\s+(\S+)", s)
        if m:
            prov.append(("go", go_ident(m.group(1))))
            continue
        if re.match(r"require\s*\($", s):
            block = True
            continue
        if block and s == ")":
            block = False
            continue
        m = re.match(r"require\s+(\S+)\s+(\S+)", s) or (block and re.match(r"(\S+)\s+(v\S+)", s))
        if m:
            req.append(("go", "go", go_ident(m.group(1)), i, m.group(2)))
    return prov, req


REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$")


def py_requirement(spec, line, file_via="python"):
    """One requirement string -> requires rows (pypi name, plus git for 'x @ git+...' / git+ URLs)."""
    s = spec.split(" #", 1)[0].strip()
    if not s or s.startswith(("#", "-r", "-c", "--")):
        return []
    rows = []
    if s.startswith("-e "):
        s = s[3:].strip()
    if re.match(r"^(git\+|https?://|ssh://)", s):
        g = git_ident(s)
        if g:
            rows.append((file_via, "git", g, line, s))
        m = re.search(r"#egg=([A-Za-z0-9._-]+)", s)
        if m:
            rows.append((file_via, "pypi", pypi_norm(m.group(1)), line, s))
        return rows
    m = REQ_NAME.match(s)
    if not m:
        return []
    rows.append((file_via, "pypi", pypi_norm(m.group(1)), line, s))
    at = re.search(r"@\s*(\S+)", m.group(3) or "")
    if at:
        g = git_ident(at.group(1))
        if g:
            rows.append((file_via, "git", g, line, s))
    return rows


def x_requirements(path, text, ctx):
    req = []
    for i, line in enumerate(text.splitlines(), 1):
        req += py_requirement(line, i)
    return [], req


def x_pyproject(path, text, ctx):
    prov, req = [], []
    if tomllib:
        try:
            d = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return [], []
        proj = d.get("project") or {}
        poetry = (d.get("tool") or {}).get("poetry") or {}
        name = proj.get("name") or poetry.get("name")
        if name:
            prov.append(("pypi", pypi_norm(name)))
        deps = list(proj.get("dependencies") or [])
        for v in (proj.get("optional-dependencies") or {}).values():
            deps += list(v)
        for spec in deps:
            req += py_requirement(spec, line_of(text, spec.split(";")[0].strip()))
        for sect in ("dependencies", "dev-dependencies"):
            for name2, v in (poetry.get(sect) or {}).items():
                if name2.lower() == "python":
                    continue
                ln = line_of(text, name2)
                req.append(("python", "pypi", pypi_norm(name2), ln, f"poetry {sect}"))
                if isinstance(v, dict) and v.get("git"):
                    g = git_ident(v["git"])
                    if g:
                        req.append(("python", "git", g, ln, v["git"]))
        return prov, req
    # Python 3.10: name = "..." under [project] / [tool.poetry], and quoted strings in dependencies = [...]
    sect, in_deps = "", False
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.startswith("["):
            sect, in_deps = s.strip("[]").strip(), False
            continue
        m = re.match(r'name\s*=\s*"([^"]+)"', s)
        if m and sect in ("project", "tool.poetry"):
            prov.append(("pypi", pypi_norm(m.group(1))))
        if sect == "project" and s.startswith("dependencies"):
            in_deps = True
        if in_deps or sect == "project.optional-dependencies":
            for spec in re.findall(r'"([^"]+)"', s):
                req += py_requirement(spec, i)
            if "]" in s:
                in_deps = False
    return prov, req


def x_setup_cfg(path, text, ctx):
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read_string(text)
    except configparser.Error:
        return [], []
    prov, req = [], []
    if cp.has_option("metadata", "name"):
        prov.append(("pypi", pypi_norm(cp.get("metadata", "name"))))
    if cp.has_option("options", "install_requires"):
        for spec in cp.get("options", "install_requires").splitlines():
            if spec.strip():
                req += py_requirement(spec, line_of(text, spec.strip()))
    return prov, req


# ---- JVM and .NET -----------------------------------------------------------------
# Maven, Gradle and sbt files depend on each other inside a repository (a module's groupId comes
# from its parent POM, `libs.x` from the version catalog, a project's group from the root script or
# a convention plugin), so their extractors only keep the text; jvm_finish resolves them together
# once every file is read. .NET and Clojure files stand alone.
SCALA_SUFFIX = re.compile(r"_(?:2\.1[0-3]|3)$")


def maven_id(g, a):
    """group:artifact, lowercase, without the Scala binary-version suffix (cats-core_2.13)."""
    return f"{g}:{SCALA_SUFFIX.sub('', a)}".lower()


def registry_url(url):
    """A package registry URL as recorded for dm-registries-static: no credentials, no query."""
    u = (url or "").strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", u, re.I) or "$" in u:
        return None
    u = re.sub(r"^([a-z][a-z0-9+.-]*://)[^@/]*@", r"\1", u, flags=re.I)
    return u.split("?", 1)[0].split("#", 1)[0].rstrip("/").lower()


def defer_jvm(path, text, ctx):
    ctx.setdefault("_jvm", {})[path] = text
    ctx.setdefault("_finish", set()).add(jvm_finish)
    return [], []


def x_pom(path, text, ctx):
    return defer_jvm(path, text, ctx)


def x_gradle(path, text, ctx):
    return defer_jvm(path, text, ctx)


def x_gradle_catalog(path, text, ctx):
    return defer_jvm(path, text, ctx)


def x_sbt(path, text, ctx):
    return defer_jvm(path, text, ctx)


def _strip_ns(root):
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def jvm_finish(ctx):
    """Every Maven / Gradle / sbt file of the repository at once: (provides, requires with file)."""
    files = ctx.get("_jvm", {})
    prov, req = [], []
    for fn, pick in ((maven_model, lambda f: os.path.basename(f) == "pom.xml"),
                     (gradle_model, lambda f: f.endswith((".gradle", ".gradle.kts", "gradle.properties", ".versions.toml"))),
                     (sbt_model, lambda f: f.endswith((".sbt", ".scala")))):
        sub = {f: t for f, t in files.items() if pick(f)}
        if sub:
            p, r = fn(sub, ctx)
            prov += p
            req += r
    return prov, req


# ---- Maven
def maven_model(poms, ctx):
    """Coordinates with ${property} resolved through the parent chain inside the repository
    (relativePath, else the parent's coordinates); parent, dependencies, managed and BOM-imported
    dependencies, plugins and extensions required; repository URLs recorded."""
    info = {}
    for path, text in poms.items():
        try:
            root = _strip_ns(ET.fromstring(text))
        except ET.ParseError:
            continue
        par = root.find("parent")
        pget = (lambda k, par=par: par.findtext(k)) if par is not None else (lambda k: None)
        props = root.find("properties")
        info[path] = {"root": root, "text": text, "g": root.findtext("groupId"), "a": root.findtext("artifactId"),
                      "v": root.findtext("version"), "pg": pget("groupId"), "pa": pget("artifactId"),
                      "pv": pget("version"), "rel": pget("relativePath"),
                      "props": {c.tag: (c.text or "").strip() for c in (props if props is not None else [])
                                if isinstance(c.tag, str)}}
    by_coord = {((i["g"] or i["pg"]), i["a"]): p for p, i in info.items()}

    def parent_of(p):
        i = info[p]
        if not i["pa"]:
            return None
        rel = i["rel"] if i["rel"] is not None else "../pom.xml"
        cand = os.path.normpath(os.path.join(os.path.dirname(p), rel or "."))
        if not cand.endswith(".xml"):
            cand = os.path.join(cand, "pom.xml")
        cand = os.path.normpath(cand)
        if cand in info and cand != p and info[cand]["a"] == i["pa"]:
            return cand
        hit = by_coord.get((i["pg"], i["pa"]))
        return hit if hit != p else None

    memo = {}

    def props_of(p, seen=()):
        if p in memo:
            return memo[p]
        i, par = info[p], parent_of(p)
        out = dict(props_of(par, seen + (p,))) if par and par not in seen else {}
        out.update(i["props"])
        g, v = i["g"] or i["pg"], i["v"] or i["pv"]
        for k, val in (("project.groupId", g), ("pom.groupId", g), ("groupId", g), ("project.artifactId", i["a"]),
                       ("project.version", v), ("version", v), ("project.parent.groupId", i["pg"]),
                       ("parent.groupId", i["pg"]), ("project.parent.artifactId", i["pa"]),
                       ("project.parent.version", i["pv"])):
            if val:
                out[k] = val
        memo[p] = out
        return out

    def res(s, pr):
        s = (s or "").strip()
        for _ in range(6):
            if "${" not in s:
                return s or None
            s = re.sub(r"\$\{([^}]+)\}", lambda m: pr.get(m.group(1), m.group(0)), s)
            if all(m.group(1) not in pr for m in re.finditer(r"\$\{([^}]+)\}", s)):
                break
        return None if "${" in s else (s or None)

    prov, req = [], []
    for path, i in info.items():
        root, text, pr = i["root"], i["text"], props_of(path)
        g, a = res(i["g"] or i["pg"], pr), res(i["a"], pr)
        if g and a:
            prov.append(("maven", maven_id(g, a)))
        pg, pa = res(i["pg"], pr), res(i["pa"], pr)
        if pg and pa:
            req.append(("maven", "maven", maven_id(pg, pa), path, line_of(text, f"<artifactId>{i['pa']}</artifactId>"),
                        "parent"))
        managed = {id(el) for dm_ in root.iter("dependencyManagement") for el in dm_.iter("dependency")}
        for dep in root.iter("dependency"):
            dg, da = res(dep.findtext("groupId"), pr), res(dep.findtext("artifactId"), pr)
            if not (dg and da):
                continue
            imp = (dep.findtext("scope") or "") == "import"
            detail = "BOM import" if imp else ("managed " if id(dep) in managed else "") + (dep.findtext("version") or "")
            req.append(("maven", "maven", maven_id(dg, da), path,
                        line_of(text, f"<artifactId>{dep.findtext('artifactId')}</artifactId>"), detail.strip()))
        for ap in root.iter("annotationProcessorPaths"):  # compiler plugin annotation processors
            for el in ap.iter("path"):
                pg_, pa_ = res(el.findtext("groupId"), pr), res(el.findtext("artifactId"), pr)
                if pg_ and pa_:
                    req.append(("maven", "maven", maven_id(pg_, pa_), path,
                                line_of(text, f"<artifactId>{el.findtext('artifactId')}</artifactId>"),
                                "annotation processor"))
        for tag in ("plugin", "extension"):
            for pl in root.iter(tag):
                pa_ = res(pl.findtext("artifactId"), pr)
                pg_ = res(pl.findtext("groupId"), pr) or ("org.apache.maven.plugins" if tag == "plugin" else None)
                if pg_ and pa_:
                    req.append(("maven", "maven", maven_id(pg_, pa_), path,
                                line_of(text, f"<artifactId>{pl.findtext('artifactId')}</artifactId>"), f"Maven {tag}"))
        dist = {id(el) for d in root.iter("distributionManagement") for el in d.iter()}
        for tag in ("repository", "pluginRepository", "snapshotRepository"):
            for el in root.iter(tag):
                url = registry_url(el.findtext("url"))
                if url:
                    where = "distributionManagement" if id(el) in dist else tag
                    req.append(("registry", "registry", url, path, line_of(text, el.findtext("url").strip()),
                                f"maven {where}"))
    return prov, req


# ---- Gradle
GRADLE_SCOPES_FOR_GROUP = {"allprojects", "subprojects", "configure", "project"}
GRADLE_CONFIG_CALL = re.compile(
    r"""(?<![\w.])([A-Za-z_]\w*|"[\w-]+")\s*\(?\s*(?:(platform|enforcedPlatform)\s*\(\s*)?"""
    r"""['"]([\w.\-]+):([\w.\-]+)(?::[^'"\s]*)?['"]""")
GRADLE_NOT_CONFIG = {"id", "version", "apply", "println", "print", "uri", "file", "files", "url", "from", "include",
                     "includeBuild", "exclude", "group", "name", "set", "mavenBundle", "module", "library"}
GRADLE_MAP_DEP = re.compile(r"""\bgroup\s*[:=]\s*['"]([\w.\-]+)['"]\s*,\s*name\s*[:=]\s*['"]([\w.\-]+)['"]""")


def gradle_lines(text):
    """[(line number, code without comments, enclosing block names)] for a Gradle script."""
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    out, stack = [], []
    for n, raw in enumerate(text.splitlines(), 1):
        code = re.sub(r"(^|\s)//.*$", r"\1", raw)
        out.append((n, code, tuple(stack)))
        bare = re.sub(r"(['\"]).*?\1", '""', code)
        for m in re.finditer(r"[{}]", bare):
            if m.group(0) == "{":
                head = re.findall(r"([A-Za-z_][\w]*)\s*(?:<[^>]*>)?\s*(?:\([^()]*\))?\s*$", bare[:m.start()])
                stack.append(head[-1] if head else "")
            elif stack:
                stack.pop()
    return out


def catalog_entries(text):
    """A version catalog's libraries, plugins and bundles: {accessor: (kind, value, line)}."""
    out, section, pending = {}, None, None
    for n, line in enumerate(text.splitlines(), 1):
        s = line.split("#", 1)[0].strip()
        m = re.match(r"^\[(\w+)\]$", s)
        if m:
            section, pending = m.group(1), None
            continue
        if pending:
            pending[1].append(s)
            if "]" in s:
                names = re.findall(r"['\"]([^'\"]+)['\"]", " ".join(pending[1]))
                out[f"bundles.{pending[0]}"] = ("bundle", names, pending[2])
                pending = None
            continue
        m = re.match(r"^([\w.\-]+)\s*=\s*(.+)$", s)
        if not m or section not in ("libraries", "plugins", "bundles"):
            continue
        key, val = re.sub(r"[-_.]", ".", m.group(1)).lower(), m.group(2)
        if section == "libraries":
            mm = re.match(r"""^['"]([\w.\-]+):([\w.\-]+)(?::[^'"]*)?['"]""", val) \
                or re.search(r"""module\s*=\s*['"]([\w.\-]+):([\w.\-]+)['"]""", val)
            if not mm:
                g, a = re.search(r"""group\s*=\s*['"]([\w.\-]+)['"]""", val), re.search(r"""name\s*=\s*['"]([\w.\-]+)['"]""", val)
                mm = (g.group(1), a.group(1)) if g and a else None
            else:
                mm = (mm.group(1), mm.group(2))
            if mm:
                out[key] = ("library", maven_id(*mm), n)
        elif section == "plugins":
            mm = re.match(r"""^['"]([\w.\-]+)(?::[^'"]*)?['"]""", val) or re.search(r"""id\s*=\s*['"]([\w.\-]+)['"]""", val)
            if mm:
                out[f"plugins.{key}"] = ("plugin", mm.group(1).lower(), n)
        else:
            names = re.findall(r"['\"]([^'\"]+)['\"]", val)
            if "]" in val:
                out[f"bundles.{key}"] = ("bundle", names, n)
            else:
                pending = (key, [val], n)
    return out


def gradle_model(files, ctx):
    """Projects (settings includes, every script directory), their group (gradle.properties, the root's
    allprojects / subprojects block, an applied convention plugin, the project's own script) and
    artifact (archivesName, publication artifactId, else the project name); dependencies as strings,
    map notation or version-catalog accessors; plugin ids applied and provided; composite builds."""
    prov, req = [], []
    scripts = {f: gradle_lines(t) for f, t in files.items() if f.endswith((".gradle", ".gradle.kts"))}
    props = {}
    for f, t in files.items():
        if f.endswith("gradle.properties"):
            kv = dict(re.findall(r"^\s*([\w.]+)\s*[=:]\s*(.*?)\s*$", t, re.M))
            props[os.path.dirname(f)] = {k.lower(): v for k, v in kv.items()}
    # build roots: every directory with a settings script (the repository root counts as one)
    settings = {os.path.dirname(f): f for f in scripts if os.path.basename(f).startswith("settings.gradle")}
    roots = sorted(set(settings) | {""}, key=len, reverse=True)

    def root_of(d):
        return next(r for r in roots if r == "" or d == r or d.startswith(r + "/"))

    catalogs = {}  # (build root, name) -> {accessor: entry}
    for f, t in files.items():
        if f.endswith(".versions.toml"):
            r = root_of(os.path.dirname(os.path.dirname(f)) if os.path.dirname(f).endswith("gradle") else os.path.dirname(f))
            name = os.path.basename(f).split(".", 1)[0]
            catalogs[(r, name)] = {k: v + (f,) for k, v in catalog_entries(t).items()}
    names, includes, projdirs = {}, {}, {}
    child_prefix, renamed = {}, {}  # settings renames: every child ("spring-ldap-" + name), or one project
    for r, sf in settings.items():
        for n, code, _stack in scripts[sf]:
            m = re.search(r"""rootProject\.name\s*=\s*['"]([^'"]+)['"]""", code)
            if m:
                names[r] = m.group(1)
            m = re.match(r"^\s*include\b(.*)$", code)
            if m:
                for pth in re.findall(r"""['"]:?([\w.\-:]+)['"]""", m.group(1)):
                    includes.setdefault(r, []).append(pth.strip(":").replace(":", "/"))
            m = re.search(r"""\b\w+\.name\s*=\s*['"]([\w.\-]+)['"]\s*\+\s*\w+""", code)
            if m:
                child_prefix[r] = m.group(1)
            m = re.search(r"""(?:findProject|project)\(\s*['"]:([\w.\-:]+)['"]\s*\)\.name\s*=\s*['"]([\w.\-]+)['"]""", code)
            if m:
                renamed[(r, m.group(1))] = m.group(2)
            m = re.search(r"""project\(\s*['"]:([\w.\-:]+)['"]\s*\)\.projectDir\s*=\s*(?:file\()?\s*['"]([^'"]+)['"]""", code)
            if m:
                pdir = re.sub(r"^\$\{?rootDir\}?/?|^\$\{?rootProject\.projectDir\}?/?", "", m.group(2)).strip("./")
                if "$" not in pdir:
                    projdirs[(r, m.group(1).replace(":", "/"))] = pdir
            for m in re.finditer(r"""\bincludeBuild\s*\(?\s*['"]([^'"]+)['"]""", code):
                tgt = os.path.normpath(os.path.join(r, m.group(1)))
                if tgt.startswith(".."):
                    sib = tgt.split("/")[tgt.split("/").count("..")] if tgt.strip("./") else ""
                    g = git_ident("../" * 1 + sib, base=ctx.get("git")) if sib else None
                    if g:
                        req.append(("gradle-build", "git", g, sf, n, f"includeBuild {m.group(1)}"))
            m = re.search(r"""create\(\s*['"](\w+)['"]\s*\)\s*\{?.*?from\(\s*['"]([\w.\-]+):([\w.\-]+)""", code)
            if m:
                req.append(("gradle", "maven", maven_id(m.group(2), m.group(3)), sf, n, "published version catalog"))
            m = re.search(r"""\blibrary\(\s*['"]([\w.\-]+)['"]\s*,\s*['"]([\w.\-]+)(?::([\w.\-]+))?['"](?:\s*,\s*['"]([\w.\-]+)['"])?""", code)
            if m:
                art = m.group(3) or m.group(4)
                if art:
                    catalogs.setdefault((r, "libs"), {})[re.sub(r"[-_.]", ".", m.group(1)).lower()] = (
                        "library", maven_id(m.group(2), art), n, sf)
            for m in re.finditer(r"""gitRepository\(\s*(?:uri|java\.net\.URI\.create)\(\s*['"]([^'"]+)['"]""", code):
                g = git_ident(m.group(1))
                if g:
                    req.append(("gradle-build", "git", g, sf, n, "sourceControl gitRepository"))

    # facts per script
    facts = {}  # script -> {"group": [(scope, value)], "artifact": [...], "plugins": [...], "pubs": [...]}
    used = set()  # (root, accessor) referenced by a script
    provided_plugins = set()
    for f, lines in scripts.items():
        r = root_of(os.path.dirname(f))
        cats = {name: cat for (cr, name), cat in catalogs.items() if cr == r}
        fx = facts.setdefault(f, {"group": [], "artifact": [], "plugins": [], "pubs": [], "applies": []})
        for n, code, stack in lines:
            inner = set(stack)
            m = re.match(r"""^\s*(?:project\.)?group\s*=?\s*['"]([^'"$]+)['"]""", code)
            if m and (not stack or inner <= GRADLE_SCOPES_FOR_GROUP):
                fx["group"].append(("all" if inner & {"allprojects"} else "sub" if inner else "own", m.group(1)))
            if m and inner & {"publications", "publishing"}:
                fx["pubs"].append(("group", m.group(1)))
            scope = "all" if "allprojects" in inner else "sub" if "subprojects" in inner else "own"
            for mm in re.finditer(r"""\bapply\s*\(?\s*from\s*[:=]\s*(?:file\()?\s*['"]([^'"]+\.gradle(?:\.kts)?)['"]""", code):
                raw_t = mm.group(1)
                if re.match(r"^\$\{?(?:rootDir|rootProject\.projectDir)\}?/", raw_t):  # the build root
                    base, tgt = r, re.sub(r"^\$\{?(?:rootDir|rootProject\.projectDir)\}?/", "", raw_t)
                else:  # $projectDir/x or a plain relative path: the script's own directory
                    base, tgt = os.path.dirname(f), re.sub(r"^\$\{?projectDir\}?/", "", raw_t)
                if "$" not in tgt and not tgt.startswith("http"):
                    fx["applies"].append((scope, os.path.normpath(os.path.join(base, tgt))))
            for m in re.finditer(r"""\b(?:archivesName\s*(?:\.set\s*\(|=)|archivesBaseName\s*=)\s*['"]([^'"]+)['"]""", code):
                fx["artifact"].append((scope, m.group(1)))
            m = re.match(r"""^\s*artifactId\s*=?\s*['"]([^'"$]+)['"]""", code)
            if m and inner & {"publications", "publishing", "create", "register"}:
                fx["pubs"].append(("artifact", m.group(1)))
            m = re.match(r"""^\s*groupId\s*=?\s*['"]([^'"$]+)['"]""", code)
            if m and inner & {"publications", "publishing", "create", "register"}:
                fx["pubs"].append(("group", m.group(1)))
            m = re.match(r"""^\s*id\s*=?\s*['"]([\w.\-]+)['"]""", code)
            if m and "gradlePlugin" in inner:
                provided_plugins.add(m.group(1).lower())
                prov.append(("gradle-plugin", m.group(1).lower()))
            elif "plugins" in inner or re.match(r"^\s*plugins\s*\{", code):
                for mm in re.finditer(r"""\bid\s*\(?\s*['"]([\w.\-]+)['"]""", code):
                    if "." in mm.group(1):
                        fx["plugins"].append(mm.group(1).lower())
                        req.append(("gradle-plugin", "gradle-plugin", mm.group(1).lower(), f, n, code.strip()[:120]))
            for mm in re.finditer(r"""apply\s*\(?\s*plugin\s*[:=]\s*['"]([\w.\-]+)['"]""", code):
                if "." in mm.group(1):
                    fx["plugins"].append(mm.group(1).lower())
                    req.append(("gradle-plugin", "gradle-plugin", mm.group(1).lower(), f, n, code.strip()[:120]))
            if not stack or "dependencies" in inner or "buildscript" in inner or "constraints" in inner:
                for mm in GRADLE_CONFIG_CALL.finditer(code):
                    if mm.group(1).strip('"') in GRADLE_NOT_CONFIG:
                        continue
                    detail = (mm.group(2) or "") + " " + code.strip()[:120]
                    req.append(("gradle", "maven", maven_id(mm.group(3), mm.group(4)), f, n, detail.strip()))
                for mm in GRADLE_MAP_DEP.finditer(code):
                    req.append(("gradle", "maven", maven_id(mm.group(1), mm.group(2)), f, n, code.strip()[:120]))
            for mm in re.finditer(r"""\buseModule\(\s*['"]([\w.\-]+):([\w.\-]+)""", code):
                # pluginManagement { resolutionStrategy { eachPlugin { useModule("g:a:v") } } }
                req.append(("gradle", "maven", maven_id(mm.group(1), mm.group(2)), f, n, "plugin " + code.strip()[:110]))
            for cname, cat in cats.items():
                refs = [mm.group(1) for mm in re.finditer(rf"\b{cname}\.((?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*)", code)]
                refs += [re.sub(r"[-_.]", ".", mm.group(1)).lower()
                         for mm in re.finditer(r"""\bfindLibrary\(\s*['"]([\w.\-]+)['"]""", code)] if cname == "libs" else []
                for ref in refs:
                    parts = ref.lower().split(".")
                    key = next((".".join(parts[:k]) for k in range(len(parts), 0, -1) if ".".join(parts[:k]) in cat), None)
                    if not key:
                        continue
                    used.add((r, cname, key))
                    kind, val, _, _src = cat[key]
                    members = [x for x in (re.sub(r"[-_.]", ".", b).lower() for b in val) if x in cat] \
                        if kind == "bundle" else []
                    used.update((r, cname, x) for x in members)
                    vals = [cat[x][1] for x in members] if kind == "bundle" else [val]
                    for v in vals:
                        if kind == "plugin":
                            fx["plugins"].append(v)
                            req.append(("gradle-plugin", "gradle-plugin", v, f, n, f"{cname}.{key}"))
                        elif isinstance(v, str):
                            req.append(("gradle", "maven", v, f, n, f"{cname}.{key} " + code.strip()[:100]))
        # a precompiled script plugin (src/main/kotlin/acme.java-conventions.gradle.kts) provides its id
        m = re.search(r"/src/main/(?:kotlin|groovy)/(?:.*/)?([\w.\-]+)\.gradle(?:\.kts)?$", "/" + f)
        if m:
            provided_plugins.add(m.group(1).lower())
            prov.append(("gradle-plugin", m.group(1).lower()))
    # an applied script (apply from: "$rootDir/gradle/versioning.gradle") counts for the script that
    # applies it, with the scope of the apply line: inside allprojects / subprojects its facts take
    # that scope (temporalio/sdk-java sets its group this way)
    for _round in range(2):
        for f, fx in facts.items():
            for scope, tgt in fx.get("applies", []):
                tf = facts.get(tgt)
                if not tf or tgt == f:
                    continue
                for key in ("group", "artifact"):
                    for sc, v in tf[key]:
                        item = (sc if scope == "own" else scope, v)
                        if item not in fx[key]:
                            fx[key].append(item)
                for item in tf["pubs"]:
                    if item not in fx["pubs"]:
                        fx["pubs"].append(item)
    # catalog entries no script read here references: still declared dependencies of the build
    for (r, cname), cat in catalogs.items():
        for key, (kind, val, n, src) in cat.items():
            if (r, cname, key) in used or kind == "bundle":
                continue
            req.append(("gradle-plugin", "gradle-plugin", val, src, n, f"version catalog {cname}.{key}") if kind == "plugin"
                       else ("gradle", "maven", val, src, n, f"version catalog {cname}.{key}"))

    # projects and their coordinates, in Gradle's order: gradle.properties, then the subprojects /
    # allprojects blocks of each ancestor script (root first), then applied convention plugins, then
    # the project's own script. A publication's groupId / artifactId wins for what is published.
    conv = {}  # convention plugin id -> its facts
    for f in scripts:
        m = re.search(r"/src/main/(?:kotlin|groovy)/(?:.*/)?([\w.\-]+)\.gradle(?:\.kts)?$", "/" + f)
        if m:
            conv[m.group(1).lower()] = facts[f]
    projects, dir_name = {}, {}
    # settings that set project.buildFileName (spring-framework, spring-security): a script named
    # <project name>.gradle is that project's build file, wherever it is
    if any("buildFileName" in files[sf] for sf in settings.values()):
        for f in scripts:
            stem = re.sub(r"\.gradle(\.kts)?$", "", os.path.basename(f))
            d = os.path.dirname(f)
            if stem in ("build", "settings") or "/src/main/" in "/" + f or d.split("/")[0] in ("buildSrc", "gradle") \
                    or os.path.basename(d) == "gradle" or f.endswith("gradle.properties"):
                continue
            if not any(os.path.join(d, b) in scripts for b in ("build.gradle", "build.gradle.kts")):
                projects.setdefault(d, root_of(d))
                dir_name[d] = stem
    for r in roots:
        projects[r] = r
        for inc in includes.get(r, []):
            d = projdirs.get((r, inc), inc)
            if d != inc:  # a remapped projectDir keeps the include's project name
                dir_name[os.path.normpath(os.path.join(r, d)) if r else os.path.normpath(d)] = inc.rsplit("/", 1)[-1]
            parts = d.split("/")
            for k in range(1, len(parts) + 1):  # include(":a:b") also makes project :a
                sub = "/".join(parts[:k]) if k < len(parts) else d
                projects.setdefault(os.path.normpath(os.path.join(r, sub)) if r else os.path.normpath(sub), r)
    for f in scripts:
        d = os.path.dirname(f)
        if os.path.basename(f) in ("build.gradle", "build.gradle.kts") and "/src/main/" not in "/" + f:
            projects.setdefault(d, root_of(d))
    # project('spring-rabbit') { ... } blocks in a build's root script declare (and configure) projects
    # that may have no script of their own (spring-amqp, spring-kafka)
    for r in roots:
        rs = next((f for f in (os.path.join(r, "build.gradle"), os.path.join(r, "build.gradle.kts")) if f in scripts), None)
        for _n, code, stack in scripts.get(rs, []):
            m = re.match(r"""^\s*project\s*\(\s*['"]:?([\w.\-:]+)['"]\s*\)\s*\{""", code)
            if m and not stack:
                projects.setdefault(os.path.normpath(os.path.join(r, m.group(1).replace(":", "/"))), r)
    # settings renames
    for d, r in list(projects.items()):
        if d == r:
            continue
        rel = os.path.relpath(d, r) if r else d
        # findProject(":spring-ldap-test-support") names the project after the prefix rename
        new = renamed.get((r, rel.replace("/", ":"))) or renamed.get((r, child_prefix.get(r, "") + os.path.basename(d)))
        if new or r in child_prefix:
            dir_name[d] = new or child_prefix[r] + os.path.basename(d)

    def script_of(d):
        named = [os.path.join(d, f"{dir_name[d]}{ext}") for ext in (".gradle.kts", ".gradle")] if d in dir_name else []
        return next((f for f in [os.path.join(d, "build.gradle.kts"), os.path.join(d, "build.gradle")] + named
                     if f in facts), None)

    def fill(tmpl, d):
        """An archivesName template ("opentelemetry-sdk-${proj.name}") for project d. A name that
        already starts with the template's prefix keeps it (the usual `if (!startsWith(...))`)."""
        name = dir_name.get(d) or (os.path.basename(d) if d else (names.get(d) or ctx.get("name") or ""))
        pat = r"\$\{?(?:project\.|proj\.|it\.)?name\}?"
        pre = re.split(pat, tmpl or "", maxsplit=1)[0]
        if pre and pre != tmpl and name.startswith(pre):
            return name
        v = re.sub(pat, name, tmpl or "")
        return None if not v or "$" in v else v

    empty = {"group": [], "artifact": [], "plugins": [], "pubs": [], "applies": []}
    for d, r in sorted(projects.items()):
        if d.split("/")[0] == "buildSrc" or d == "buildSrc":
            continue
        fx = facts.get(script_of(d), empty)
        chain = [a for a in [r] + ["/".join(d.split("/")[:k]) for k in range(1, d.count("/") + 1)]
                 if a != d and (a == r or a.startswith(r + "/") or r == "")]
        group = next((props[a]["group"] for a in [d] + chain[::-1] if a in props and props[a].get("group")), None)
        art = None
        for a in dict.fromkeys(chain):
            afx = facts.get(script_of(a), empty)
            for sc, v in afx["group"]:
                if sc in ("all", "sub"):
                    group = v
            for sc, v in afx["artifact"]:
                if sc in ("all", "sub"):
                    art = fill(v, d) or art
        for sc, v in facts.get(script_of(r), empty)["group"]:
            if d == r and sc in ("all", "own"):
                group = v
        pub_group = None
        for pid in fx["plugins"]:
            c = conv.get(pid)
            if not c:
                continue
            group = next((v for sc, v in c["group"] if sc == "own"), group)
            pub_group = dict(c["pubs"]).get("group", pub_group)
            if not art:
                art = next((fill(v, d) for sc, v in c["artifact"] if sc == "own" and fill(v, d)), None)
        own_group = [v for sc, v in fx["group"] if sc == "own"]
        if own_group and d != r:
            group = own_group[-1]
        if not group and d != r:
            # last resort: the root project's own group, which convention plugins usually copy to
            # every project (spring-security: project.group = rootProject.group)
            group = next((v for sc, v in facts.get(script_of(r), empty)["group"] if sc in ("own", "all")), None)
        own_art = [fill(v, d) for sc, v in fx["artifact"] if sc == "own" and fill(v, d)]
        art = (own_art[-1] if own_art else None) or art or props.get(d, {}).get("archivesbasename") \
            or (names.get(r) or ctx.get("name") if d == r else dir_name.get(d) or os.path.basename(d))
        pubs = dict(fx["pubs"])
        g = pubs.get("group") or pub_group or group
        if g and art:
            prov.append(("maven", maven_id(g, art)))
        for k, v in fx["pubs"]:
            if k == "artifact" and g:
                prov.append(("maven", maven_id(g, v)))
    return prov, req


# ---- sbt
SBT_DEP = re.compile(r'"([\w.\-]+)"\s*(%%%|%%|%)\s*"([\w.\-]+)"')


def sbt_model(files, ctx):
    """organization + each project name provided; `"g" %% "a" % v` dependencies and sbt plugins;
    RootProject / ProjectRef URIs to other repositories."""
    prov, req, orgs, names = [], [], [], []
    for f, t in files.items():
        for n, line in enumerate(t.splitlines(), 1):
            code = re.sub(r"(^|\s)//.*$", r"\1", line)
            m = re.search(r"""\borganization\s*(?:in\s+ThisBuild\s*)?:=\s*"([^"$]+)\"""", code)
            if m:
                orgs.append(m.group(1))
            for mm in re.finditer(r"""\bname\s*:=\s*"([^"$]+)\"""", code):
                names.append(mm.group(1))
            for mm in re.finditer(r"""\blazy\s+val\s+(\w+)\s*=\s*\(?\s*(?:project\b|Project\()""", code):
                if mm.group(1) != "root":
                    names.append(re.sub(r"(?<!^)(?=[A-Z])", "-", mm.group(1)).lower())
            for mm in SBT_DEP.finditer(code):
                kind = "sbt plugin" if "addSbtPlugin" in code else mm.group(2)
                req.append(("sbt", "maven", maven_id(mm.group(1), mm.group(3)), f, n, f"{kind} {code.strip()[:100]}"))
            for mm in re.finditer(r"""\b(?:RootProject|ProjectRef)\s*\(\s*uri\(\s*"([^"]+)"\s*\)""", code):
                g = git_ident(mm.group(1))
                if g:
                    req.append(("sbt", "git", g, f, n, code.strip()[:120]))
    for o in dict.fromkeys(orgs):
        for nm in dict.fromkeys(names):
            prov.append(("maven", maven_id(o, nm)))
    return prov, req


# ---- Clojure
def x_deps_edn(path, text, ctx):
    req = []
    for m in re.finditer(r"([\w.\-]+(?:/[\w.\-]+)?)\s*\{([^{}]*)\}", text):
        lib, body = m.group(1), m.group(2)
        n = text.count("\n", 0, m.start()) + 1
        url = re.search(r':git/url\s+"([^"]+)"', body)
        if url or ":git/sha" in body or ":git/tag" in body:
            g = git_ident(url.group(1)) if url else None
            if not g:  # tools.deps infers io.github.ORG/REPO -> github.com/ORG/REPO
                mm = re.match(r"^(?:io|com)\.(github|gitlab)\.([\w\-]+)/([\w.\-]+)$", lib)
                g = norm_path(f"{mm.group(1)}.com/{mm.group(2)}/{mm.group(3)}") if mm else None
            if g:
                req.append(("clojure", "git", g, n, f"{lib} {body.strip()[:80]}"))
        elif ":mvn/version" in body:
            grp, _, art = lib.partition("/")
            req.append(("clojure", "maven", maven_id(grp, art or grp), n, body.strip()[:80]))
    return [], req


def x_project_clj(path, text, ctx):
    prov, req = [], []
    m = re.search(r"\(defproject\s+([\w.\-]+(?:/[\w.\-]+)?)\s", text)
    if m:
        grp, _, art = m.group(1).partition("/")
        prov.append(("maven", maven_id(grp, art or grp)))
    for mm in re.finditer(r"\[([\w.\-]+(?:/[\w.\-]+)?)\s+\"[^\"]+\"", text[m.end() if m else 0:]):
        grp, _, art = mm.group(1).partition("/")
        off = (m.end() if m else 0) + mm.start()
        req.append(("clojure", "maven", maven_id(grp, art or grp), text.count("\n", 0, off) + 1, mm.group(0)[:80]))
    return prov, req


# ---- .NET
def nuget_id(name):
    return (name or "").strip().lower()


def x_msbuild(path, text, ctx):
    """*.csproj / *.fsproj / *.vbproj and Directory.*.props / .targets: PackageReference and
    GlobalPackageReference required, PackageId (else AssemblyName, else the file name) provided,
    a ProjectReference that leaves the repository required as the sibling repository."""
    try:
        root = _strip_ns(ET.fromstring(text))
    except ET.ParseError:
        return [], []
    prov, req = [], []
    b = os.path.basename(path)
    if not b.startswith("Directory."):
        pid = root.findtext(".//PackageId") or root.findtext(".//AssemblyName") or os.path.splitext(b)[0]
        if pid and "$(" not in pid:
            prov.append(("nuget", nuget_id(pid)))
    for tag in ("PackageReference", "GlobalPackageReference"):
        for el in root.iter(tag):
            name = el.get("Include")
            if name and "$(" not in name:
                ver = el.get("Version") or el.findtext("Version") or ""
                req.append(("nuget", "nuget", nuget_id(name), line_of(text, f'"{name}"'), f"{tag} {ver}".strip()))
    for el in root.iter("ProjectReference"):
        inc = (el.get("Include") or "").replace("\\", "/")
        tgt = os.path.normpath(os.path.join(os.path.dirname(path), inc))
        if tgt.startswith("../"):
            sib = [x for x in tgt.split("/") if x != ".."][0]
            g = git_ident(f"../{sib}", base=ctx.get("git"))
            if g:
                req.append(("dotnet-project", "git", g, line_of(text, el.get("Include")), f"ProjectReference {inc}"))
    return prov, req


def x_packages_config(path, text, ctx):
    req = []
    for m in re.finditer(r"""<package\s[^>]*\bid\s*=\s*["']([^"']+)["']""", text):
        req.append(("nuget", "nuget", nuget_id(m.group(1)), text.count("\n", 0, m.start()) + 1, "packages.config"))
    return [], req


def x_paket(path, text, ctx):
    """paket.dependencies (nuget / github / git / source lines) and paket.references (package names)."""
    req = []
    refs = os.path.basename(path).lower().startswith("paket.references")
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()  # comments start a line (// or #); a URL carries // of its own
        parts = s.split()
        if not parts or parts[0].startswith(("#", "//")):
            continue
        if refs:
            if parts[0] not in ("group", "File:") and re.match(r"^[\w.\-]+$", parts[0]):
                req.append(("nuget", "nuget", nuget_id(parts[0]), n, "paket.references"))
        elif parts[0] == "nuget" and len(parts) > 1:
            req.append(("nuget", "nuget", nuget_id(parts[1]), n, s[:100]))
        elif parts[0] in ("github", "gist") and len(parts) > 1:
            g = git_ident(f"github.com/{parts[1].split(':', 1)[0]}") if parts[0] == "github" else None
            if g:
                req.append(("paket", "git", "/".join(g.split("/")[:3]), n, s[:100]))
        elif parts[0] == "git" and len(parts) > 1:
            g = git_ident(parts[1])
            if g:
                req.append(("paket", "git", g, n, s[:100]))
        elif parts[0] == "source" and len(parts) > 1:
            u = registry_url(parts[1])
            if u:
                req.append(("registry", "registry", u, n, "paket source"))
    return [], req


def x_nuget_config(path, text, ctx):
    req = []
    for m in re.finditer(r"""<add\s[^>]*\bvalue\s*=\s*["']([^"']+)["']""", text):
        u = registry_url(m.group(1))
        if u:
            req.append(("registry", "registry", u, text.count("\n", 0, m.start()) + 1, "nuget packageSource"))
    return [], req


# ---- C, C++, Rust, Swift, Bazel and others ----------------------------------------
def archive_repo(url):
    """A source archive URL on a code host -> the repository it is cut from (host/org/repo), else
    None: github.com/O/R/archive/..., /releases/download/..., codeload.github.com/O/R/...,
    GitLab .../-/archive/..., Gitea / Forgejo O/R/archive/..."""
    u = (url or "").strip().strip("'\"")
    m = re.match(r"^[a-z]+://(?:[^@/]+@)?([^/]+)/(.+?)/-/archive/", u, re.I)  # GitLab, any group depth
    if m:
        return norm_path(f"{m.group(1).split(':')[0]}/{m.group(2)}")
    m = re.match(r"^[a-z]+://codeload\.github\.com/([^/]+)/([^/]+)/", u, re.I)
    if m:
        return norm_path(f"github.com/{m.group(1)}/{m.group(2)}")
    m = re.match(r"^[a-z]+://(?:[^@/]+@)?([^/]+)/([^/]+)/([^/]+)/(?:archive|releases/download)/", u, re.I)
    if m:
        return norm_path(f"{m.group(1).split(':')[0]}/{m.group(2)}/{m.group(3)}")
    return None


def source_ident(url, base=None):
    """A git URL or a source archive URL -> repository identity."""
    return archive_repo(url) or git_ident(url, base=base)


def _ln(text, pos):
    return text.count("\n", 0, pos) + 1


def _strip_hash_comments(text):
    """Drop # comments (CMake, Starlark, TOML-ish) outside quotes, keeping line breaks."""
    out = []
    for line in text.splitlines():
        q, cut = None, len(line)
        for i, ch in enumerate(line):
            if q:
                q = None if ch == q else q
            elif ch in "\"'":
                q = ch
            elif ch == "#":
                cut = i
                break
        out.append(line[:cut])
    return "\n".join(out)


# ---- CMake
CMAKE_FETCH = re.compile(r"\b(FetchContent_Declare|ExternalProject_Add|CPMAddPackage|CPMDeclarePackage)\s*\(([^)]*)\)",
                         re.I | re.S)


def x_cmake(path, text, ctx):
    """CMakeLists.txt / *.cmake: FetchContent_Declare, ExternalProject_Add and CPM sources (git or a
    source archive; cmake-fetch); find_package(X) as `cmake` x; project(X) and XConfig.cmake files
    provide `cmake` x."""
    prov, req = [], []
    code = _strip_hash_comments(text)
    var = {m.group(1): m.group(2) for m in re.finditer(r"\bset\s*\(\s*(\w+)\s+\"?([^\s\")]+)\"?", code, re.I)}

    def expand(v):
        v = re.sub(r"\$\{(\w+)\}", lambda m: var.get(m.group(1), m.group(0)), v.strip().strip('"'))
        return None if "${" in v else v
    for m in CMAKE_FETCH.finditer(code):
        body, n = m.group(2), _ln(code, m.start())
        args = re.findall(r'"[^"]*"|\S+', body)
        url = None
        for k in ("GIT_REPOSITORY", "URL", "GITHUB_REPOSITORY", "GITLAB_REPOSITORY"):
            if k in args and args.index(k) + 1 < len(args):
                url = expand(args[args.index(k) + 1])
                if url and k == "GITHUB_REPOSITORY":
                    url = f"github.com/{url}"
                elif url and k == "GITLAB_REPOSITORY":
                    url = f"gitlab.com/{url}"
                break
        if not url and args and m.group(1).lower().startswith("cpm"):
            short = re.match(r'^"?(gh|gl|bb):([\w.\-]+/[\w.\-]+)', args[0])  # "gh:fmtlib/fmt@10.0.0"
            if short:
                host = {"gh": "github.com", "gl": "gitlab.com", "bb": "bitbucket.org"}[short.group(1)]
                url = f"{host}/{short.group(2)}"
        g = source_ident(url) if url else None
        if g:
            req.append(("cmake-fetch", "git", g, n, f"{m.group(1)} {url}"[:120]))
    for m in re.finditer(r"\bfind_package\s*\(\s*([\w.\-+]+)", code, re.I):
        req.append(("cmake", "cmake", m.group(1).lower(), _ln(code, m.start()), m.group(0)[:80]))
    if os.path.basename(path) == "CMakeLists.txt":
        m = re.search(r"\bproject\s*\(\s*([\w.\-+]+)", code, re.I)
        if m:
            prov.append(("cmake", m.group(1).lower()))
    m = re.match(r"^([\w.\-+]+?)(?:Config|-config)\.cmake(?:\.in)?$", os.path.basename(path))
    if m:
        prov.append(("cmake", m.group(1).lower()))
    for m in re.finditer(r"\binstall\s*\(\s*EXPORT\s+([\w.\-+]+?)(?:Targets|-targets)?\b", code):
        prov.append(("cmake", m.group(1).lower()))
    return prov, req


# ---- Conan, vcpkg, Meson
def x_conan(path, text, ctx):
    """conanfile.txt [requires] / [tool_requires], conanfile.py requires / self.requires(); name provided."""
    prov, req = [], []
    if path.endswith(".txt"):
        sect = ""
        for n, line in enumerate(text.splitlines(), 1):
            s = line.split("#", 1)[0].strip()
            if s.startswith("["):
                sect = s.strip("[]")
            elif s and sect in ("requires", "tool_requires", "build_requires", "test_requires"):
                req.append(("conan", "conan", s.split("/", 1)[0].lower(), n, s[:80]))
        return prov, req
    code = _strip_hash_comments(text)
    m = re.search(r"^\s*name\s*=\s*['\"]([\w.\-+]+)['\"]", code, re.M)
    if m:
        prov.append(("conan", m.group(1).lower()))
    for m in re.finditer(r"""\b(?:requires|tool_requires|build_requires|test_requires)\s*(?:=|\()\s*([^\n)]*)""", code):
        for ref in re.findall(r"""['"]([\w.\-+]+)/[^'"]*['"]""", m.group(1)):
            req.append(("conan", "conan", ref.lower(), _ln(code, m.start()), m.group(0).strip()[:80]))
    return prov, req


def x_vcpkg(path, text, ctx):
    """vcpkg.json dependencies (name provided); vcpkg-configuration.json git registries."""
    try:
        d = json.loads(text)
    except ValueError:
        return [], []
    prov, req = [], []
    if os.path.basename(path) == "vcpkg.json":
        if d.get("name"):
            prov.append(("vcpkg", str(d["name"]).lower()))
        for dep in d.get("dependencies") or []:
            name = dep if isinstance(dep, str) else (dep.get("name") if isinstance(dep, dict) else None)
            if name:
                req.append(("vcpkg", "vcpkg", name.lower(), line_of(text, f'"{name}"'), "vcpkg dependency"))
        return prov, req
    regs = list(d.get("registries") or []) + ([d["default-registry"]] if isinstance(d.get("default-registry"), dict) else [])
    for r in regs:
        if not isinstance(r, dict):
            continue
        loc = r.get("repository") or r.get("location") or ""
        if r.get("kind") == "git":
            g = git_ident(loc)
            if g:
                req.append(("vcpkg", "git", g, line_of(text, loc), f"vcpkg git registry for {', '.join(r.get('packages') or [])}"[:120]))
        elif r.get("kind") == "artifact" and registry_url(loc):
            req.append(("registry", "registry", registry_url(loc), line_of(text, loc), "vcpkg artifact registry"))
    return prov, req


def x_meson_wrap(path, text, ctx):
    """subprojects/*.wrap: [wrap-git] url, [wrap-file] source_url on a code host."""
    req = []
    for m in re.finditer(r"^[ \t]*(url|source_url)\s*=\s*(\S+)", text, re.M):
        g = source_ident(m.group(2)) if m.group(1) == "source_url" else git_ident(m.group(2))
        if g:
            req.append(("meson", "git", g, _ln(text, m.start()), m.group(2)[:120]))
    return [], req


# ---- Rust
def crate_name(name):
    """crates.io treats - and _ in a crate name as the same character."""
    return (name or "").strip().lower().replace("_", "-")


def toml_tables(text):
    """A forgiving line-based TOML reader for Cargo.toml: [(table, key, value text, line)], with
    [dependencies.serde] sub-table keys reported under table "dependencies" and key "serde"."""
    out, table = [], ""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        s = raw.split(" #", 1)[0].strip() if not raw.strip().startswith("#") else ""
        n = i + 1
        i += 1
        if not s:
            continue
        m = re.match(r"^\[\[?\s*([^\]]+?)\s*\]\]?$", s)
        if m:
            table = m.group(1).replace('"', "").replace("'", "")
            continue
        m = re.match(r"""^(["']?)([\w.\-]+)\1\s*=\s*(.*)$""", s)
        if not m:
            continue
        val = m.group(3)
        while val.count("{") > val.count("}") or val.count("[") > val.count("]"):
            if i >= len(lines):
                break
            val += " " + lines[i].split(" #", 1)[0].strip()
            i += 1
        out.append((table, m.group(2), val, n))
    return out


CARGO_DEP_TABLE = re.compile(r"^(?:workspace\.)?(?:target\.[^.]+(?:\.[^.]+)*\.)?(dependencies|dev-dependencies|build-dependencies)$")


def x_cargo(path, text, ctx):
    """Cargo.toml: [package] name provided as `crates`; each dependency (all sections, target
    tables, workspace.dependencies, [dependencies.x] tables, `package =` renames) required as
    `crates`, a `git =` source as `git`, a `path =` that leaves the repository as the sibling
    repository; [patch.*] git sources."""
    prov, req = [], []
    rows = toml_tables(text)
    sub = {}  # [dependencies.x] tables: (section, crate) -> {key: value}
    for table, key, val, n in rows:
        if table == "package" and key == "name":
            prov.append(("crates", crate_name(val.strip("\"'"))))
        m = re.match(r"^(.*(?:dependencies))\.([\w\-]+)$", table)
        if m and CARGO_DEP_TABLE.match(m.group(1)):
            sub.setdefault((m.group(1), m.group(2)), {"_line": n})[key] = val.strip()

    def dep(name, spec, n, section):
        git = re.search(r"""\bgit\s*=\s*["']([^"']+)["']""", spec)
        pth = re.search(r"""\bpath\s*=\s*["']([^"']+)["']""", spec)
        pkg = re.search(r"""\bpackage\s*=\s*["']([^"']+)["']""", spec)
        reg = re.search(r"""\bregistry\s*=\s*["']([^"']+)["']""", spec)
        real = crate_name(pkg.group(1) if pkg else name)
        if git:
            g = git_ident(git.group(1))
            if g:
                req.append(("cargo", "git", g, n, f"{section} {name} git {git.group(1)}"[:120]))
        if pth:
            tgt = os.path.normpath(os.path.join(os.path.dirname(path), pth.group(1)))
            if tgt.startswith("../"):
                sib = [x for x in tgt.split("/") if x != ".."][0]
                g = git_ident(f"../{sib}", base=ctx.get("git"))
                if g:
                    req.append(("cargo", "git", g, n, f"{section} {name} path {pth.group(1)}"[:120]))
            return  # a path dependency inside the repository is the repository itself
        if not git:
            req.append(("cargo", "crates", real, n, f"{section} {spec[:60]}" + (f" registry {reg.group(1)}" if reg else "")))
    for table, key, val, n in rows:
        m = CARGO_DEP_TABLE.match(table)
        if m:
            dep(key, val, n, m.group(1))
        elif table.startswith("patch."):
            g = re.search(r"""\bgit\s*=\s*["']([^"']+)["']""", val)
            if g and git_ident(g.group(1)):
                req.append(("cargo", "git", git_ident(g.group(1)), n, f"[{table}] {key}"))
    for (section, name), kv in sub.items():
        dep(name, " ".join(f"{k} = {v}" for k, v in kv.items() if k != "_line"), kv["_line"], section)
    return prov, req


def x_cargo_config(path, text, ctx):
    req = []
    for table, key, val, n in toml_tables(text):
        if table.startswith("registries.") and key == "index":
            u = registry_url(val.strip("\"'").removeprefix("sparse+"))
            if u:
                req.append(("registry", "registry", u, n, f"cargo registry {table.split('.', 1)[1]}"))
    return [], req


# ---- Swift and Apple
def x_swift_package(path, text, ctx):
    req = []
    for m in re.finditer(r"""\.package\s*\(\s*(?:name:\s*"[^"]*"\s*,\s*)?(url|path):\s*"([^"]+)\"""", text):
        kind, v = m.group(1), m.group(2)
        if kind == "url":
            g = git_ident(v)
        else:
            tgt = os.path.normpath(os.path.join(os.path.dirname(path), v))
            g = git_ident(f"../{[x for x in tgt.split('/') if x != '..'][0]}", base=ctx.get("git")) \
                if tgt.startswith("../") else None
        if g:
            req.append(("swiftpm", "git", g, _ln(text, m.start(2)), m.group(0)[:120]))
    return [], req


def x_podfile(path, text, ctx):
    """Podfile pods (`cocoapods` name, `:git =>` as git); *.podspec name provided."""
    prov, req = [], []
    if path.endswith(".podspec"):
        m = re.search(r"""\.name\s*=\s*['"]([\w.\-+]+)['"]""", text)
        if m:
            prov.append(("cocoapods", m.group(1).lower()))
        return prov, req
    for m in re.finditer(r"""^[ \t]*pod\s+['"]([\w.\-+/]+)['"]([^\n]*)""", text, re.M):
        n, rest = _ln(text, m.start()), m.group(2)
        g = re.search(r""":git\s*=>\s*['"]([^'"]+)['"]|git:\s*['"]([^'"]+)['"]""", rest)
        if g:
            gi = git_ident(g.group(1) or g.group(2))
            if gi:
                req.append(("cocoapods", "git", gi, n, m.group(0).strip()[:120]))
        else:
            req.append(("cocoapods", "cocoapods", m.group(1).split("/", 1)[0].lower(), n, m.group(0).strip()[:120]))
    return prov, req


def x_cartfile(path, text, ctx):
    req = []
    for m in re.finditer(r"""^[ \t]*(github|git)\s+"([^"]+)\"""", text, re.M):
        g = git_ident(f"github.com/{m.group(2)}") if m.group(1) == "github" and "://" not in m.group(2) else git_ident(m.group(2))
        if g:
            req.append(("carthage", "git", g, _ln(text, m.start()), m.group(0).strip()[:120]))
    return [], req


# ---- Bazel
def _starlark_calls(code, names):
    """(function, {kwarg: value}, offset) for each call of these functions: string and list kwargs."""
    for m in re.finditer(rf"\b({'|'.join(names)})\s*\(", code):
        depth, i = 1, m.end()
        while i < len(code) and depth:
            depth += {"(": 1, ")": -1}.get(code[i], 0)
            i += 1
        body = code[m.end():i - 1]
        kw = {}
        for k in re.finditer(r"\b(\w+)\s*=\s*(\"[^\"]*\"|'[^']*'|\[[^\]]*\])", body):
            v = k.group(2)
            kw[k.group(1)] = re.findall(r"[\"']([^\"']*)[\"']", v) if v.startswith("[") else v[1:-1]
        yield m.group(1), kw, m.start()


def x_bazel(path, text, ctx):
    """MODULE.bazel: module(name) provided, bazel_dep required as `bazel`, git_override /
    archive_override sources; WORKSPACE and *deps*.bzl: git_repository / new_git_repository remote,
    http_archive urls on a code host; workspace(name) provided."""
    if re.search(r"(^|/)modules/[^/]+/[^/]+/MODULE\.bazel$", path):
        return [], []  # a registry entry (bazel-central-registry layout): another module's copy
    prov, req = [], []
    code = _strip_hash_comments(text)
    calls = ("module", "bazel_dep", "git_override", "archive_override", "workspace", "git_repository",
             "new_git_repository", "http_archive")
    for fn, kw, off in _starlark_calls(code, calls):
        n = _ln(code, off)
        if fn in ("module", "workspace") and isinstance(kw.get("name"), str):
            prov.append(("bazel", kw["name"].lower()))
        elif fn == "bazel_dep" and isinstance(kw.get("name"), str):
            req.append(("bazel", "bazel", kw["name"].lower(), n, f"bazel_dep {kw.get('version', '')}".strip()))
        elif fn in ("git_override", "git_repository", "new_git_repository") and isinstance(kw.get("remote"), str):
            g = git_ident(kw["remote"])
            if g:
                req.append(("bazel", "git", g, n, f"{fn} {kw.get('module_name') or kw.get('name') or ''}".strip()))
        elif fn in ("archive_override", "http_archive"):
            urls = kw.get("urls") or ([kw["url"]] if isinstance(kw.get("url"), str) else [])
            g = next((archive_repo(u) for u in urls if archive_repo(u)), None)
            if g:
                req.append(("bazel", "git", g, n, f"{fn} {kw.get('module_name') or kw.get('name') or ''}".strip()))
    return prov, req


# ---- Haskell, Zig, Elixir, Erlang
def x_other_git_deps(path, text, ctx):
    """cabal.project source-repository-package, build.zig.zon urls, mix.exs git / github deps,
    rebar.config git deps."""
    req, b = [], os.path.basename(path)
    if b.startswith("cabal.project"):
        for m in re.finditer(r"^[ \t]*location:\s*(\S+)", text, re.M):
            g = git_ident(m.group(1))
            if g:
                req.append(("cabal", "git", g, _ln(text, m.start()), m.group(0).strip()[:120]))
    elif b == "build.zig.zon":
        for m in re.finditer(r'\.url\s*=\s*"([^"]+)"', text):
            u = re.sub(r"^git\+", "", m.group(1)).split("#", 1)[0]
            g = archive_repo(u) or git_ident(u)
            if g:
                req.append(("zig", "git", g, _ln(text, m.start()), m.group(0)[:120]))
    elif b == "mix.exs":
        for m in re.finditer(r"""\{\s*:(\w+)\s*,[^}]*?\b(git|github):\s*"([^"]+)\"""", text):
            g = git_ident(m.group(3) if m.group(2) == "git" else f"github.com/{m.group(3)}")
            if g:
                req.append(("mix", "git", g, _ln(text, m.start()), m.group(0)[:120]))
    elif b == "rebar.config":
        for m in re.finditer(r"""\{\s*(\w+)\s*,\s*(?:"[^"]*"\s*,\s*)?\{\s*git\s*,\s*"([^"]+)\"""", text):
            g = git_ident(m.group(2))
            if g:
                req.append(("rebar", "git", g, _ln(text, m.start()), m.group(0)[:120]))
    return [], req


def yaml_list_of_maps(text, key):
    """Items of a top-level `key:` list of maps, line-based: [(line, {k: v})]. Enough for
    Chart.yaml dependencies; not a YAML parser."""
    items, inside, cur, ind = [], False, None, None
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if re.match(rf"^{key}\s*:", line):
            inside = True
            continue
        if inside and re.match(r"^\S", line):
            break
        if not inside:
            continue
        m = re.match(r"^(\s*)-\s+(\w[\w-]*)\s*:\s*(.*)$", line)
        if m:
            cur = {m.group(2): m.group(3).strip().strip("'\"")}
            ind = len(m.group(1))
            items.append((i, cur))
            continue
        m = re.match(r"^(\s*)(\w[\w-]*)\s*:\s*(.*)$", line)
        if m and cur is not None and len(m.group(1)) > (ind or 0):
            cur[m.group(2)] = m.group(3).strip().strip("'\"")
    return items


def x_chart(path, text, ctx):
    prov, req = [], []
    m = re.search(r"^name\s*:\s*['\"]?([\w.\-]+)", text, re.M)
    if m:
        prov.append(("helm", m.group(1).lower()))
    for ln, item in yaml_list_of_maps(text, "dependencies"):
        name, repo = item.get("name"), item.get("repository", "")
        if name and not repo.startswith("file://"):
            req.append(("helm", "helm", name.lower(), ln, repo))
            g = git_ident(repo) if repo.startswith("oci://") else None
            if g:
                req.append(("helm", "image", g.split("/", 1)[1], ln, repo))
    return prov, req


TF_SOURCE = re.compile(r'^\s*source\s*=\s*"([^"]+)"')


def x_terraform(path, text, ctx):
    req, in_module = [], False
    for i, line in enumerate(text.splitlines(), 1):
        if re.match(r'^\s*module\s+"', line):
            in_module = True
        elif re.match(r"^\S", line) and not line.startswith("}"):
            in_module = False
        m = TF_SOURCE.match(line)
        if not (m and in_module):
            continue
        src = m.group(1)
        if src.startswith(("./", "../")):
            continue
        parts = src.split("//", 1)[0].split("?", 1)[0].split("/")
        if re.match(r"^(git::|git@|https?://|ssh://|github\.com/|bitbucket\.org/)", src) or src.endswith(".git"):
            g = git_ident(src)
            if g:
                req.append(("terraform", "git", g, i, src))
        elif len(parts) == 3 and "." not in parts[0]:
            req.append(("terraform", "tfreg", "/".join(parts).lower(), i, src))
        elif len(parts) == 4 and "." in parts[0]:
            req.append(("terraform", "tfreg", "/".join(parts[1:]).lower(), i, src))  # private registry host
        elif len(parts) >= 3 and "." in parts[0]:
            g = git_ident(src)
            if g:
                req.append(("terraform", "git", g, i, src))
    return [], req


def tf_provides(repo):
    """terraform-<provider>-<name> -> tfreg:<owner>/<name>/<provider> (the registry convention)."""
    m = re.match(r"^terraform-([a-z0-9]+)-(.+)$", repo["name"].lower())
    if not m:
        return []
    owner = repo["path"].lower().split("/")[-2] if "/" in repo["path"] else ""
    return [("tfreg", f"{owner}/{m.group(2)}/{m.group(1)}")]


def ci_value(s):
    return s.split("#", 1)[0].strip().strip("'\"")


def x_gitlab_ci(path, text, ctx):
    req = []
    ns = ctx["path"].rsplit("/", 1)[0] if "/" in ctx["path"] else ""
    subst = {"$CI_PROJECT_NAMESPACE": ns, "${CI_PROJECT_NAMESPACE}": ns, "$CI_SERVER_HOST": ctx["host"],
             "${CI_SERVER_HOST}": ctx["host"], "$CI_SERVER_FQDN": ctx["host"], "${CI_SERVER_FQDN}": ctx["host"]}

    def expand(v):
        for k, val in subst.items():
            v = v.replace(k, val)
        return None if "$" in v else v

    lines = text.splitlines()
    for i, line in enumerate(lines, 1):
        m = re.match(r"^(\s*)(?:-\s+)?(project|component|remote|trigger)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(2), expand(ci_value(m.group(3)))
        if not val:
            continue
        if key == "project":
            parent = ""
            for j in range(i - 2, -1, -1):  # nearest less-indented key: trigger: or include:
                pm = re.match(r"^(\s*)(?:-\s+)?([\w-]+)\s*:", lines[j])
                if pm and len(pm.group(1)) < len(m.group(1)):
                    parent = pm.group(2)
                    break
            via = "ci-trigger" if parent == "trigger" else "ci-include"
            g = norm_path(f"{ctx['host']}/{val}")
            if g:
                req.append((via, "git", g, i, val))
        elif key == "component":
            p = val.split("@", 1)[0].rsplit("/", 1)[0]
            g = norm_path(p)
            if g:
                req.append(("ci-component", "git", g, i, val))
        elif key == "remote":
            if "/-/raw/" in val:
                g = git_ident(val.split("/-/raw/", 1)[0])
                if g:
                    req.append(("ci-include", "git", g, i, val))
        elif key == "trigger" and "/" in val and ":" not in val:
            g = norm_path(f"{ctx['host']}/{val}")
            if g:
                req.append(("ci-trigger", "git", g, i, val))
    return [], req


def x_workflow(path, text, ctx):
    """GitHub / Gitea Actions `uses:`: reusable workflows, actions, docker:// images."""
    req = []
    # Gitea resolves bare owner/repo through DEFAULT_ACTIONS_URL; the instance itself is the one
    # that can match a repository of this map ('self'), so bare names are looked up there.
    host = ctx["host"]
    for i, line in enumerate(text.splitlines(), 1):
        m = re.match(r"^\s*(?:-\s+)?uses\s*:\s*(.+)$", line)
        if not m:
            continue
        v = ci_value(m.group(1))
        if not v or v.startswith("./") or "${{" in v:
            continue
        if v.startswith("docker://"):
            img = image_path(v[len("docker://"):])
            if img:
                req.append(("action", "image", img, i, v))
            continue
        ref = v.split("@", 1)[0]
        if re.match(r"^https?://", ref):
            g = git_ident(ref)
            via = "workflow" if "/.github/workflows/" in ref or "/.gitea/workflows/" in ref else "action"
        else:
            parts = ref.split("/")
            if len(parts) < 2:
                continue
            g = norm_path(f"{host}/{parts[0]}/{parts[1]}")
            via = "workflow" if re.search(r"/\.(github|gitea)/workflows/", ref) else "action"
        if g:
            if via == "workflow" and "/.github/workflows/" in g:
                g = g.split("/.github/workflows/", 1)[0]
            req.append((via, "git", g, i, v))
    return [], req


def image_path(ref):
    """Image reference -> registry-agnostic repository path (no tag/digest), lowercased."""
    ref = ref.strip().split("@", 1)[0]
    parts = ref.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        parts = parts[1:]
    if not parts:
        return None
    parts[-1] = parts[-1].split(":", 1)[0]
    p = "/".join(parts).lower()
    if "/" not in p:
        p = f"library/{p}"
    return p


def x_dockerfile(path, text, ctx):
    req, stages, args = [], set(), {}
    lines = text.replace("\\\n", " ").splitlines()
    for i, line in enumerate(lines, 1):
        s = line.strip()
        m = re.match(r"(?i)^ARG\s+(\w+)(?:=(\S+))?", s)
        if m and m.group(2):
            args[m.group(1)] = m.group(2).strip("'\"")
            continue
        m = re.match(r"(?i)^FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?", s)
        if not m:
            continue
        img = re.sub(r"\$\{?(\w+)\}?", lambda mm: args.get(mm.group(1), "$"), m.group(1))
        if m.group(2):
            stages.add(m.group(2).lower())
        if "$" in img or img.lower() == "scratch" or img.lower() in stages:
            continue
        p = image_path(img)
        if p:
            req.append(("image", "image", p, line_of(text, m.group(0).split()[1]) or i, img))
    return [], req


# Directories whose manifests are not the repository's own dependencies: vendored code, test data,
# examples, and test source sets (spring-boot's maven plugin keeps 304 fixture projects under
# src/intTest/, src/test/).
SKIP_DIRS = re.compile(r"(^|/)(node_modules|vendor|third_party|\.terraform|testdata|fixtures|__fixtures__|_?examples?|samples?"
                       r"|src/(?:it|test|intTest|integrationTest|functionalTest|testFixtures))/")


def extractor_for(path):
    """The extractor that reads this path, or None when it is not a manifest."""
    b = os.path.basename(path)
    bl = b.lower()
    if path == ".gitmodules":
        return x_gitmodules
    if b == "package.json":
        return x_package_json
    if b == "go.mod":
        return x_go_mod
    if b == "pyproject.toml":
        return x_pyproject
    if b == "setup.cfg":
        return x_setup_cfg
    if bl.startswith("requirements") and bl.endswith(".txt"):
        return x_requirements
    if b == "pom.xml":
        return x_pom
    if b.endswith((".gradle", ".gradle.kts")) or b == "gradle.properties":
        return x_gradle
    if bl.endswith(".versions.toml"):
        return x_gradle_catalog
    if b.endswith(".sbt") or (path.startswith("project/") and b.endswith(".scala") and path.count("/") == 1):
        return x_sbt
    if b == "deps.edn":
        return x_deps_edn
    if b == "project.clj":
        return x_project_clj
    if bl.endswith((".csproj", ".fsproj", ".vbproj")) or bl in ("directory.packages.props", "directory.build.props",
                                                                  "directory.build.targets"):
        return x_msbuild
    if bl == "packages.config":
        return x_packages_config
    if bl in ("paket.dependencies", "paket.references"):
        return x_paket
    if bl == "nuget.config":
        return x_nuget_config
    if b == "CMakeLists.txt" or bl.endswith(".cmake") or bl.endswith(("config.cmake.in", "-config.cmake.in")):
        return x_cmake
    if bl in ("conanfile.txt", "conanfile.py"):
        return x_conan
    if b in ("vcpkg.json", "vcpkg-configuration.json"):
        return x_vcpkg
    if bl.endswith(".wrap") and "subprojects/" in "/" + path:
        return x_meson_wrap
    if b == "Cargo.toml":
        return x_cargo
    if re.search(r"(^|/)\.cargo/config(\.toml)?$", path):
        return x_cargo_config
    if b == "Package.swift":
        return x_swift_package
    if b == "Podfile" or bl.endswith(".podspec"):
        return x_podfile
    if b in ("Cartfile", "Cartfile.private"):
        return x_cartfile
    if b in ("MODULE.bazel", "WORKSPACE", "WORKSPACE.bazel") or (bl.endswith(".bzl") and re.search(r"(deps|repositories|workspace)", bl)):
        return x_bazel
    if b.startswith("cabal.project") or b in ("build.zig.zon", "mix.exs", "rebar.config"):
        return x_other_git_deps
    if b == "Chart.yaml":
        return x_chart
    if b.endswith(".tf"):
        return x_terraform
    if path == ".gitlab-ci.yml" or (path.startswith(".gitlab/") and bl.endswith((".yml", ".yaml"))):
        return x_gitlab_ci
    if re.match(r"^\.(github|gitea)/workflows/[^/]+\.ya?ml$", path) or b in ("action.yml", "action.yaml"):
        return x_workflow
    if bl in ("dockerfile", "containerfile") or bl.startswith("dockerfile.") or bl.endswith(".dockerfile"):
        return x_dockerfile
    return None


def plan_files(files, max_files=MAX_FILES):
    """The manifests to read from a tree: ([(path, extractor)], manifests found, [paths too large]).

    Under the cap, the first manifest of each kind in a directory comes before a second one of that
    kind there, and the kinds take turns, so 700 build.gradle files cannot crowd out a repository's
    Dockerfiles and workflows. The picked files are returned shallowest first (the order they are read).
    """
    found, large = [], []
    for path, size in sorted(files, key=lambda f: (f[0].count("/"), f[0])):
        if SKIP_DIRS.search(path):
            continue
        x = extractor_for(path)
        if not x:
            continue
        if (size or 0) > MAX_FILE_BYTES:
            large.append(path)
            continue
        found.append((path, x))
    in_dir, in_kind, keyed = {}, {}, []
    for path, x in found:
        d = (x, os.path.dirname(path))
        in_dir[d] = in_dir.get(d, -1) + 1
        in_kind[x] = in_kind.get(x, -1) + 1
        keyed.append(((in_dir[d], in_kind[x], path.count("/"), path), path, x))
    picked = [(p, x) for _, p, x in sorted(keyed, key=lambda k: k[0])[:max(0, max_files)]]
    picked.sort(key=lambda px: (px[0].count("/"), px[0]))
    return picked, len(found), large


def select_files(files, max_files=MAX_FILES):
    """Pick the manifest files worth reading from a repository's tree: [(path, extractor)]."""
    return plan_files(files, max_files)[0]


TABLE_OR_LIST = re.compile(r"^\s*(?:\||[*+-]\s|\d+\.\s)")


def md_inline(s):
    """Markdown inline markup removed: images, link targets, emphasis, code ticks, HTML tags."""
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"[*`]+|(?<!\w)_+|_+(?!\w)", "", s)  # `_` only as emphasis: snake_case names stay searchable
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def lead_in_items(lines):
    """The items of a table (first column of each body row) or list that a lead-in ending in ':' introduces."""
    if not lines:
        return ""
    if lines[0].lstrip().startswith("|"):
        rows = [l.strip().strip("|").split("|")[0] for l in lines[1:] if l.lstrip().startswith("|")]
        items = [md_inline(r) for r in rows if not re.fullmatch(r"[\s:-]*", r)]
        return ", ".join(i for i in items if i)
    items = []
    for l in lines:
        if re.match(r"^\s*(?:[*+-]|\d+\.)\s+", l):
            items.append(re.sub(r"^\s*(?:[*+-]|\d+\.)\s+", "", l))
        elif items:
            items[-1] += " " + l.strip()  # a wrapped list item
    return "; ".join(i for i in (md_inline(i).rstrip(".;, ") for i in items) if i)


def readme_summary(text, limit=300):
    """First prose paragraph of a README, markdown stripped, at most `limit` characters. A paragraph
    ending in ':' is followed by the table or list it introduces ("Manages: a, b, c.")."""
    text = text.lstrip("\ufeff")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    paras = re.split(r"\n\s*\n", text)
    for i, p in enumerate(paras):
        lines = [l for l in p.strip().splitlines() if l.strip()]
        if not lines:
            continue
        if all(re.match(r"^\s*(#|=+$|-+$|\[!\[|!\[|<|\||>|\*\s*\[|-\s*\[)", l) for l in lines):
            continue
        # a row of links ("English | 中文 | ..."): navigation, not prose
        if len(re.findall(r"\]\([^)]*\)", p)) >= 2 and " | " in p:
            continue
        lines = [l for l in lines if not re.match(r"^\s*(#|=+$|-+$|\[!\[|!\[)", l)]
        if not lines:
            continue
        # a lead-in line ending in ':' with its table or list right under it, in the same paragraph
        block = []
        for j in range(1, len(lines)):
            if TABLE_OR_LIST.match(lines[j]) and lines[j - 1].rstrip().endswith(":"):
                lines, block = lines[:j], lines[j:]
                break
        # list markers and headings inside them ("* #### A script that ...")
        lines = [re.sub(r"^\s*(?:[*+-]|\d+\.)\s+(?:#+\s*)?", "", l) for l in lines]
        s = md_inline(" ".join(l.strip() for l in lines))
        if s.endswith(":"):
            if not block and i + 1 < len(paras):
                nxt = [l for l in paras[i + 1].strip().splitlines() if l.strip()]
                block = nxt if nxt and TABLE_OR_LIST.match(nxt[0]) else []
            items = lead_in_items(block)
            if items:
                s = f"{s} {items}."
        if len(s) < 20:
            continue
        return s if len(s) <= limit else s[:limit - 1].rsplit(" ", 1)[0] + "…"
    return ""


# --------------------------------------------------------------------------------- build
DDL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE repos(path TEXT PRIMARY KEY, name TEXT, description TEXT, topics TEXT, language TEXT,
  default_branch TEXT, archived INT, fork INT, last_activity TEXT, web_url TEXT, clone_https TEXT,
  clone_ssh TEXT, readme TEXT, files TEXT, note TEXT, scanned_at TEXT);
CREATE TABLE provides(repo TEXT, ns TEXT, ident TEXT);
CREATE TABLE requires(repo TEXT, via TEXT, ns TEXT, ident TEXT, file TEXT, line INT, detail TEXT);
CREATE TABLE subgroups(path TEXT PRIMARY KEY);
CREATE TABLE gaps(repo TEXT, kind TEXT, found INT, read INT, detail TEXT);
CREATE INDEX provides_ident ON provides(ns, ident);
CREATE INDEX requires_repo ON requires(repo);
"""

# Why a repository's scan is incomplete. found / read are counts where the kind has them.
GAP_KINDS = {
    "manifest-cap": "read {read} of {found} manifests ({detail})",
    "tree-truncated": "{detail}; deeper files not seen",
    "file-too-large": "{found} manifest(s) over 512 KB not read: {detail}",
    "scan-failed": "scan failed: {detail}",
}


def gap_text(g):
    return GAP_KINDS.get(g["kind"], "{kind}: {detail}").format(**g)


def graph_dir(host, org, cache=None):
    safe_host = host.replace(":", "_")
    return os.path.join(cache or CACHE, safe_host, *org.split("/"))


def graph_path(host, org, cache=None):
    return os.path.join(graph_dir(host, org, cache), "map.db")


def extract(texts, ctx):
    """Run the extractors over {path: text}, then the repository-wide finishers they registered
    (jvm_finish): (provides, requires [(via, ns, ident, file, line, detail)])."""
    provides, requires = [], []
    for path, text in texts.items():
        x = extractor_for(path)
        if not x:
            continue
        p, r = x(path, text, ctx)
        provides += p
        requires += [(via, ns, ident, path, line, detail) for via, ns, ident, line, detail in r]
    for fn in sorted(ctx.get("_finish", ()), key=lambda f: f.__name__):
        p, r = fn(ctx)
        provides += p
        requires += r
    return provides, requires


def scan_repo(prov, repo, max_files=MAX_FILES, max_tree_entries=MAX_TREE_ENTRIES):
    """Read a repository's tree and manifest files: (provides, requires, readme, files, note, gaps).

    gaps: [(kind, found, read, detail)], everything that left the scan incomplete (GAP_KINDS)."""
    host = prov.host
    # identities never carry a port (https and ssh ports differ for the same repository)
    ctx = {"host": host.lower().split(":", 1)[0], "path": repo["path"], "provider": prov.name, "name": repo["name"],
           "git": git_ident(repo["clone_https"]) or norm_path(f"{host}/{repo['path']}")}
    provides = [("git", ctx["git"])] + tf_provides(repo)
    if repo.get("empty") or not repo.get("default_branch"):
        return provides, [], "", [], "empty repository", []
    files, cut = prov.tree(repo, max_tree_entries)
    gaps = [("tree-truncated", len(files), None, cut)] if cut else []
    note, requires, readme, read = "", [], "", []
    rd = next((p for p, _ in files if "/" not in p and re.match(r"(?i)^readme(\.(md|markdown|rst|txt))?$", p)), None)
    if rd:
        try:
            readme = readme_summary(prov.read(repo, rd))
        except NotFound:
            pass
    picked, found, large = plan_files(files, max_files)
    if found > len(picked):
        gaps.append(("manifest-cap", found, len(picked), f"--max-files {max_files}"))
    if large:
        more = f" (+{len(large) - 5} more)" if len(large) > 5 else ""
        gaps.append(("file-too-large", len(large), 0, ", ".join(large[:5]) + more))
    texts = {}
    for path, _x in picked:
        try:
            texts[path] = prov.read(repo, path)
        except NotFound:
            continue
        read.append(path)
    p, requires = extract(texts, ctx)
    provides += p
    if any(extractor_for(f) is x_dockerfile for f in texts):
        provides.append(("image", repo["path"].lower()))
    # drop duplicates, keep the first file:line of each
    seen, req = set(), []
    for row in requires:
        k = row[:3]
        if k not in seen:
            seen.add(k)
            req.append(row)
    return sorted(set(provides)), req, readme, read, note, gaps


def build_graph(t, provider, host, org, cache=None, jobs=8, max_files=MAX_FILES, full=False, parent="",
                log=print, max_tree_entries=MAX_TREE_ENTRIES, internal=None):
    """Build (or incrementally refresh) one graph; returns (path, stats, subgroup paths).

    A repository is rescanned when it changed, when its last scan failed, and every repository is
    when the scanner version or the limits differ from the last build's. internal: ["ns:prefix"]
    replaces the stored list; None keeps it."""
    prov = PROVIDER_CLASS[provider](t, host)
    t0 = time.time()
    repos = prov.list_repos(org)
    subs = prov.subgroups(org)
    dbp = graph_path(host, org, cache)
    limits = {"scanner": str(SCANNER), "max_files": str(max_files), "max_tree_entries": str(max_tree_entries)}
    old, ometa = {}, {}
    if os.path.exists(dbp):
        try:
            odb = sqlite3.connect(dbp)
            ometa = dict(odb.execute("SELECT key, value FROM meta"))
            changed = [f"{k} {ometa.get(k, '1' if k == 'scanner' else '?')} -> {v}" for k, v in limits.items()
                       if ometa.get(k) != v]
            if ometa.get("schema") != str(SCHEMA) or full:
                odb.close()
            elif changed:
                log(f"{host}/{org}: {', '.join(changed)}: rescanning every repository")
                odb.close()
            else:
                for row in odb.execute("SELECT path, last_activity, default_branch, note FROM repos"):
                    old[row[0]] = row[1:]
                old["__db__"] = odb
        except sqlite3.Error:
            old = {}
    if internal is None:
        internal = json.loads(ometa.get("internal") or "[]")
    stats = {"repos": len(repos), "scanned": 0, "reused": 0, "failed": 0, "requires": 0, "incomplete": 0}
    results = {}

    def work(r):
        if provider == "gitea" and r.get("topics") is None:
            r["topics"] = prov.topics(r)
        o = old.get(r["path"])
        if o and o[0] == r["last_activity"] and o[1] == r["default_branch"] and not (o[2] or "").startswith("error"):
            return r, None
        if provider == "gitlab":
            r["language"] = prov.language(r)
        try:
            return r, scan_repo(prov, r, max_files, max_tree_entries)
        except ApiError as e:
            if isinstance(e, AuthError):
                raise
            err = str(e)[:300]
            return r, ([("git", git_ident(r["clone_https"]) or "")], [], "", [], f"error: {err}",
                       [("scan-failed", None, None, err)])

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        for r, res in ex.map(work, repos):
            results[r["path"]] = (r, res)

    os.makedirs(os.path.dirname(dbp), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dbp), prefix=".map-", suffix=".db")
    os.close(fd)
    db = sqlite3.connect(tmp)
    db.executescript(DDL)
    odb = old.get("__db__")
    stamp = iso(now())
    for path, (r, res) in sorted(results.items()):
        if res is None:  # unchanged since the last build: copy its rows
            orow = odb.execute("SELECT language, readme, files, note, scanned_at, topics FROM repos WHERE path=?", (path,)).fetchone()
            lang, readme, files, note, scanned = orow[:5]
            if provider == "gitlab":
                r["language"] = lang
            if r.get("topics") is None:
                r["topics"] = json.loads(orow[5] or "[]")
            for table, cols in (("provides", 3), ("requires", 7), ("gaps", 5)):
                db.executemany(f"INSERT INTO {table} VALUES ({','.join('?' * cols)})",
                               odb.execute(f"SELECT * FROM {table} WHERE repo=?", (path,)))
            stats["reused"] += 1
        else:
            provides, requires, readme, files, note, gaps = res
            files, scanned = json.dumps(files), stamp
            db.executemany("INSERT INTO provides VALUES (?,?,?)", [(path, ns, i) for ns, i in provides if i])
            db.executemany("INSERT INTO requires VALUES (?,?,?,?,?,?,?)", [(path,) + row for row in requires])
            db.executemany("INSERT INTO gaps VALUES (?,?,?,?,?)", [(path,) + g for g in gaps])
            stats["failed" if note.startswith("error") else "scanned"] += 1
        db.execute("INSERT INTO repos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            path, r["name"], r["description"], json.dumps(sorted(r.get("topics") or [])), r["language"],
            r["default_branch"], int(r["archived"]), int(r["fork"]), r["last_activity"], r["web_url"],
            r["clone_https"], r["clone_ssh"], readme, files, note, scanned))
    stats["requires"] = db.execute("SELECT COUNT(*) FROM requires").fetchone()[0]
    stats["incomplete"] = db.execute("SELECT COUNT(DISTINCT repo) FROM gaps").fetchone()[0]
    db.executemany("INSERT INTO subgroups VALUES (?)", [(s,) for s in subs])
    meta = {"schema": str(SCHEMA), "provider": provider, "host": host, "org": org, "parent": parent,
            "built_at": stamp, "seconds": f"{time.time() - t0:.1f}", "api_calls": str(t.calls),
            "internal": json.dumps(sorted(set(internal))), **limits}
    db.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
    db.commit()
    db.close()
    if odb:
        odb.close()
    os.replace(tmp, dbp)
    return dbp, stats, subs


def parse_target(target, provider=None):
    """'https://host/org[/sub]', 'host/org' or 'org' (with --provider github/gitlab) -> (provider, host, org)."""
    t = re.sub(r"^[a-z]+://", "", target.strip()).strip("/")
    if "/" not in t:
        host = {"github": "github.com", "gitlab": "gitlab.com", "gitea": "gitea.com"}.get(provider or "github")
        org = t
    else:
        first, rest = t.split("/", 1)
        if "." in first or ":" in first or first == "localhost":
            host, org = first, rest
        else:
            host = {"github": "github.com", "gitlab": "gitlab.com", "gitea": "gitea.com"}.get(provider or "github")
            org = t
    provider = provider or KNOWN_HOSTS.get(host)
    if not provider:
        raise SystemExit(f"dungeon-map: cannot tell which provider {host} runs; pass --provider github|gitlab|gitea")
    if provider == "github":
        org = org.split("/")[0]
    return provider, host, org


INTERNAL_NS = ("git", "go", "npm", "pypi", "maven", "image", "tfreg", "helm", "nuget", "gradle-plugin", "crates",
               "cmake", "conan", "vcpkg", "cocoapods", "bazel")


def cmd_build(a):
    bad = [e for e in a.internal or [] if e.partition(":")[0] not in INTERNAL_NS or not e.partition(":")[2]]
    if bad:
        print(f"dungeon-map: --internal takes NS:PREFIX with NS one of {', '.join(INTERNAL_NS)}: {', '.join(bad)}",
              file=sys.stderr)
        return 2
    provider, host, org = parse_target(a.target, a.provider)
    cache = a.cache
    if a.from_dir:
        with open(os.path.join(a.from_dir, "responses.json"), encoding="utf-8") as f:
            t = ReplayTransport(json.load(f))
    else:
        t = CliTransport(provider, host, timeout=a.timeout)
    if a.save:
        t = RecordingTransport(t)
    todo, built = [(org, "")], []
    try:
        while todo:
            o, parent = todo.pop(0)
            dbp, st, subs = build_graph(t, provider, host, o, cache=cache, jobs=a.jobs, max_files=a.max_files,
                                        full=a.full, parent=parent, max_tree_entries=a.max_tree_entries,
                                        internal=a.internal)
            built.append(o)
            inc = f", {st['incomplete']} incomplete" if st["incomplete"] else ""
            print(f"{host}/{o}: {st['repos']} repos ({st['scanned']} scanned, {st['reused']} unchanged, "
                  f"{st['failed']} failed{inc}), {st['requires']} references -> {dbp}")
            if subs and a.subgroups:
                todo += [(s, o) for s in subs]
            elif subs:
                print(f"  {len(subs)} subgroups not built (--no-subgroups): {', '.join(subs[:5])}")
    except AuthError as e:
        print(f"dungeon-map: {e}", file=sys.stderr)
        return 3
    except NotFound as e:
        print(f"dungeon-map: {host}/{org} not found ({e}); check the org/group path and your access", file=sys.stderr)
        return 2
    except ApiError as e:
        print(f"dungeon-map: {e}", file=sys.stderr)
        return 2
    finally:
        if a.save and isinstance(t, RecordingTransport):
            os.makedirs(a.save, exist_ok=True)
            with open(os.path.join(a.save, "responses.json"), "w", encoding="utf-8") as f:
                json.dump(t.saved, f, indent=1, sort_keys=True)
    m = load_map(cache)
    gks = {f"{host.lower()}/{o}" for o in built}
    n_edges = sum(1 for e in m.edges if e["src_graph"] in gks)
    print(f"map: {len(m.graphs)} graphs, {len(m.repos)} repos, {len(m.edges)} cross-repository edges "
          f"({n_edges} from what was just built)")
    n_inc = sum(1 for r in m.repos.values() if r["graph"] in gks and r["gaps"])
    n_unr = sum(1 for u in m.unresolved if u["src_graph"] in gks)
    if n_inc or n_unr:
        print(f"gaps: {n_inc} repositories read incompletely, {n_unr} internal references with no "
              f"repository in the map; list them: run.sh gaps")
    if m.unresolved_hint:
        print(m.unresolved_hint)
    return 0


# --------------------------------------------------------------------------------- read side
class Map:
    """Every cached graph loaded at once, with requires resolved to edges between repositories."""

    def __init__(self):
        self.graphs = {}   # "host/org" -> meta dict (+ path)
        self.repos = {}    # "host/path" -> repo dict (+ graph)
        self.edges = []    # {src, dst, via, ns, ident, file, line, detail, match, src_graph}
        self.unresolved_hint = ""
        self.unresolved = []  # internal requires that resolve to no repository (internal_rules)
        self.internal_hints = []  # [(ns, prefix, packages)]: shared names worth --internal
        # references to a self-hosted host of the map, in a group / org that is not built
        self.outside = []  # {src, via, kind, ns, ident, file, line, host, provider, group}

    def out_edges(self, key):
        return [e for e in self.edges if e["src"] == key]

    def in_edges(self, key):
        return [e for e in self.edges if e["dst"] == key]

    def unresolved_from(self, key):
        return [u for u in self.unresolved if u["src"] == key]

    def resolve(self, name):
        """A repository key from 'host/path', 'path', a URL, or a bare name -> [keys]."""
        n = name.strip().rstrip("/")
        g = git_ident(n) if ("://" in n or n.startswith("git@")) else None
        if g and g in self.repos:
            return [g]
        n = n.lower().removesuffix(".git")
        if n in self.repos:
            return [n]
        hits = [k for k in self.repos if k.split("/", 1)[1] == n]
        if hits:
            return hits
        return [k for k in self.repos if k.rsplit("/", 1)[1] == n]


def find_graphs(cache=None):
    root = cache or CACHE
    out = []
    for d, _, files in os.walk(root):
        if "map.db" in files:
            out.append(os.path.join(d, "map.db"))
    return sorted(out)


def load_map(cache=None, hosts=None):
    m = Map()
    provides = {}  # (ns, ident) -> {repo keys}
    requires = []
    extra = []     # the per-map internal prefixes (build --internal NS:PREFIX)
    for p in find_graphs(cache):
        try:
            db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            meta = dict(db.execute("SELECT key, value FROM meta"))
        except sqlite3.Error:
            continue
        if meta.get("schema") != str(SCHEMA) or (hosts and meta["host"] not in hosts):
            db.close()
            continue
        host, gk = meta["host"].lower(), f"{meta['host'].lower()}/{meta['org']}"
        meta["path"] = p
        meta["subgroups"] = [r[0] for r in db.execute("SELECT path FROM subgroups")]
        cols = [c[1] for c in db.execute("PRAGMA table_info(repos)")]
        n = 0
        for row in db.execute("SELECT * FROM repos"):
            r = dict(zip(cols, row, strict=True))
            r["topics"] = json.loads(r["topics"] or "[]")
            r["files"] = json.loads(r["files"] or "[]")
            r["graph"], r["host"], r["provider"] = gk, meta["host"], meta["provider"]
            key = f"{host}/{r['path'].lower()}"
            r["key"] = key
            m.repos[key] = r
            n += 1
        meta["repos"] = n
        m.graphs[gk] = meta
        for r in m.repos.values():
            r.setdefault("gaps", [])
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='gaps'").fetchone():
            for repo, kind, found, read, detail in db.execute("SELECT * FROM gaps"):
                r = m.repos.get(f"{host}/{repo.lower()}")
                if r is not None:
                    r["gaps"].append({"kind": kind, "found": found, "read": read, "detail": detail})
        extra += json.loads(meta.get("internal") or "[]")
        for repo, ns, ident in db.execute("SELECT repo, ns, ident FROM provides"):
            provides.setdefault((ns, ident), set()).add(f"{host}/{repo.lower()}")
        for repo, via, ns, ident, f, line, detail in db.execute("SELECT * FROM requires"):
            requires.append((f"{host}/{repo.lower()}", via, ns, ident, f, line, detail, gk))
        db.close()

    by_ns = {}
    for (ns, ident), keys in provides.items():
        by_ns.setdefault(ns, {})[ident] = keys

    def lookup(ns, ident):
        idx = by_ns.get(ns, {})
        if ident in idx:
            return idx[ident], "exact"
        if ns in ("git", "go", "image"):
            parts = ident.split("/")
            for i in range(len(parts) - 1, 1 if ns != "image" else 0, -1):
                pre = "/".join(parts[:i])
                if pre in idx:
                    return idx[pre], "prefix"
        if ns == "go":  # go module paths are usually repository URLs
            hit, how = lookup("git", ident)
            if hit:
                return hit, how
        return set(), ""

    seen = set()
    internal, m.internal_hints = internal_rules(m, provides, extra)
    # self-hosted hosts of the map (identities carry no port): host -> (host as built, provider)
    own_hosts = {g["host"].lower().split(":", 1)[0]: (g["host"], g["provider"]) for g in m.graphs.values()
                 if g["host"].lower().split(":", 1)[0] not in SHARED_GO_HOSTS}
    for src, via, ns, ident, f, line, detail, gk in requires:
        dsts, how = lookup(ns, ident)
        if len(dsts) > 1:
            # One name, several repositories: an archived old home (opentelemetry-js-api still
            # provides @opentelemetry/api), a mirror or a fork. Prefer the live, non-fork ones.
            live = {d for d in dsts if not (m.repos.get(d, {}).get("archived") or m.repos.get(d, {}).get("fork"))}
            dsts = live or dsts
        why = "" if dsts else internal(ns, ident)
        kind = VIA_KIND.get(via, "build")
        row = {"src": src, "via": via, "kind": kind, "ns": ns, "ident": ident, "file": f, "line": line,
               "detail": detail, "src_graph": gk}
        # a short `uses: owner/repo@v` names no host: Gitea fetches it from DEFAULT_ACTIONS_URL
        # (github.com unless configured), so only a full URL counts as this host's
        short_use = via in ("action", "workflow") and "://" not in (detail or "")
        if dsts or (src, ns, ident) in seen:
            pass
        elif why:
            seen.add((src, ns, ident))
            m.unresolved.append({**row, "why": why})
        elif ns in ("git", "go") and not short_use and ident.count("/") >= 2 and ident.split("/", 1)[0] in own_hosts:
            seen.add((src, ns, ident))
            host, prov = own_hosts[ident.split("/", 1)[0]]
            m.outside.append({**row, "host": host, "provider": prov, "group": ident.split("/")[1]})
        for dst in sorted(dsts):
            if dst == src or (src, dst, via) in seen:
                continue
            seen.add((src, dst, via))
            m.edges.append({"src": src, "dst": dst, "via": via, "kind": kind, "ns": ns, "ident": ident, "file": f,
                            "line": line, "detail": detail, "match": how, "src_graph": gk})
    return m


def name_token(name):
    """The leading word of a package name: opentelemetry-sdk -> opentelemetry."""
    return re.split(r"[-_.]", name, maxsplit=1)[0] if re.search(r"[-_.]", name) else ""


def internal_rules(m, provides, extra=()):
    """(internal, hints): internal(ns, ident) says why ident is the org's own ("" when it is not);
    hints are --internal prefixes worth suggesting.

    The org's namespaces come from what its non-fork repositories provide (a fork carries its
    upstream's names): the host/org path of each graph (git, go), the org path as an image path,
    Go vanity domains, npm scopes, Maven groupIds (and their children), Terraform registry owners,
    and extra ("ns:prefix", from build --internal). A shared leading name is not a namespace: on
    PyPI or unscoped npm anyone can publish opentelemetry-instrumentation-foo (traceloop publishes
    34 of them), so it is only suggested, as ("pypi", "opentelemetry-", packages), when it matches
    the org's name and two or more of the org's packages carry it."""
    rules = {}  # ns -> {prefix: why}

    def add(ns, prefix, why):
        rules.setdefault(ns, {}).setdefault(prefix, why)

    org_words = set()
    for meta in m.graphs.values():
        h, org = meta["host"].lower().split(":", 1)[0], meta["org"].lower()
        for ns in ("git", "go"):
            add(ns, f"{h}/{org}/", f"under {h}/{org}/")
        add("image", f"{org}/", f"image path {org}/")
        for seg in org.split("/"):
            org_words |= {re.sub(r"[-_.]", "", seg), re.split(r"[-_.]", seg)[0]}
    shared = SHARED_GO_HOSTS | {g["host"].lower().split(":", 1)[0] for g in m.graphs.values()}
    tokens = {}  # (ns, token) -> {names}
    vanity = {}  # Go vanity domain -> first path segments the org's modules use
    for (ns, ident), keys in provides.items():
        if all(m.repos.get(k, {}).get("fork") for k in keys):
            continue
        if ns == "go" and "/" in ident and ident.split("/", 1)[0] not in shared:
            vanity.setdefault(ident.split("/", 1)[0], set()).add(ident.split("/")[1])
        elif ns == "npm" and ident.startswith("@") and "/" in ident:
            sc = ident.split("/", 1)[0]
            add("npm", f"{sc}/", f"npm scope {sc} is the org's")
        elif ns == "maven" and ":" in ident:
            g = ident.split(":", 1)[0]
            add("maven", f"{g}:", f"Maven group {g} is the org's")
            add("maven", f"{g}.", f"Maven group {g} is the org's")
            add("gradle-plugin", f"{g}.", f"Gradle plugin ids under the org's Maven group {g}")
        elif ns == "nuget" and "." in ident and ident.split(".", 1)[0] in org_words:
            # NuGet reserves an ID prefix for its owner: Acme.* is acme's
            pre = ident.split(".", 1)[0]
            add("nuget", f"{pre}.", f"NuGet prefix {pre}. is the org's")
        elif ns == "tfreg" and "/" in ident:
            o = ident.split("/", 1)[0]
            add("tfreg", f"{o}/", f"Terraform registry namespace {o} is the org's")
        elif ns in ("npm", "pypi", "crates") and name_token(ident) in org_words:
            tokens.setdefault((ns, name_token(ident)), set()).add(ident)
    for d, segs in vanity.items():
        # go.opentelemetry.io/{otel,contrib,...} is open-telemetry's; google.golang.org/grpc alone does
        # not make google.golang.org (protobuf, genproto, api) grpc's
        if len(segs) >= 2:
            add("go", f"{d}/", f"Go modules under {d}/ are the org's")
        else:
            add("go", f"{d}/{next(iter(segs))}/", f"Go modules under {d}/{next(iter(segs))}/ are the org's")
    for e in extra:
        ns, _, pre = e.partition(":")
        if pre:
            add(ns, pre.lower(), f"internal prefix {pre} (build --internal)")
    hints = sorted((ns, f"{tok}-", len(names)) for (ns, tok), names in tokens.items()
                   if len(names) >= 2 and f"{tok}-" not in rules.get(ns, {}))

    def internal(ns, ident):
        if ns == "pypi":
            ident = pypi_norm(ident)
        for pre, why in rules.get(ns, {}).items():
            if ident.startswith(pre):
                return why
        return ""
    return internal, hints


def age_text(stamp):
    t = parse_time(stamp)
    if not t:
        return "unknown age"
    s = (now() - t).total_seconds()
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    return f"{int(s // 86400)} days ago"


def unresolved_text(u):
    return f"{u['ident']} ({u['via']}, {u['file']}:{u['line']}; {u['why']})"


def build_command(host, group, provider, run="run.sh"):
    """The dungeon-map command that adds a group / org to the map."""
    prov = "" if KNOWN_HOSTS.get(host.lower()) == provider else f" --provider {provider}"
    return f"{run} build {host}/{group}{prov}"


def outside_text(o, run="run.sh"):
    return (f"{o['ident']} ({o['via']}, {o['file']}:{o['line']}): {o['host']}/{o['group']} is not in the map; "
            f"add it: {build_command(o['host'], o['group'], o['provider'], run)}")


def cmd_gaps(a):
    """Everything that makes the map incomplete: repositories read in part, and the org's own
    references that no repository in the map provides."""
    m = load_map(a.cache)
    if not m.graphs:
        print(f"no dungeon map in {a.cache or CACHE}; build one: run.sh build https://github.com/ORG")
        return 1
    want = [n.lower().rstrip("/") for n in a.names]
    keys = set()
    for n in want:
        hit = [k for k, r in m.repos.items() if n in (k, r["graph"].lower(), r["graph"].split("/", 1)[1].lower())]
        hit = hit or m.resolve(n)
        if not hit:
            print(f"no graph or repository {n} in the map (run.sh maps lists the graphs)")
            return 1
        keys.update(hit)
    total = 0
    for gk in sorted(m.graphs):
        repos = sorted(k for k, r in m.repos.items() if r["graph"] == gk and (not keys or k in keys))
        inc = [k for k in repos if m.repos[k]["gaps"]]
        unr = [u for u in m.unresolved if u["src"] in repos]
        out = [o for o in m.outside if o["src"] in repos]
        if not inc and not unr and not out:
            continue
        total += len(inc) + len(unr) + len(out)
        print(f"{gk}: {len(inc)} repositories read incompletely, {len(unr)} internal references with no repository")
        for k in inc:
            for g in m.repos[k]["gaps"]:
                print(f"  {k.split('/', 1)[1]}: {gap_text(g)}")
        if unr:
            print("  internal references that resolve to no repository in the map:")
            for u in sorted(unr, key=lambda u: (u["src"], u["ident"])):
                print(f"    {u['src'].split('/', 1)[1]} -> {unresolved_text(u)}")
        if out:
            print("  references into groups / orgs of this host that are not in the map:")
            for o in sorted(out, key=lambda o: (o["src"], o["ident"])):
                print(f"    {o['src'].split('/', 1)[1]} -> {outside_text(o)}")
    if not total:
        print("no gaps: every repository was read completely and every internal reference resolves")
    for ns, pre, n in m.internal_hints:
        print(f"note: {n} of the org's {ns} packages are named {pre}*; {ns} has no namespaces, so "
              f"references to other {pre}* packages are not reported. If only the org publishes "
              f"that prefix, rebuild with --internal {ns}:{pre}")
    return 0


def cmd_maps(a):
    m = load_map(a.cache)
    if not m.graphs:
        print(f"no dungeon map in {a.cache or CACHE}; build one: run.sh build https://github.com/ORG")
        return 1
    print(f"{len(m.graphs)} graphs in {a.cache or CACHE}:")
    for gk, meta in sorted(m.graphs.items()):
        par = f", subgroup of {meta['host']}/{meta['parent']}" if meta.get("parent") else ""
        subs = f", {len(meta['subgroups'])} subgroups" if meta["subgroups"] else ""
        out = sum(1 for e in m.edges if e["src_graph"] == gk)
        inc = sum(1 for r in m.repos.values() if r["graph"] == gk and r["gaps"])
        unr = sum(1 for u in m.unresolved if u["src_graph"] == gk)
        gaps = f", gaps: {inc} incomplete, {unr} unresolved" if inc or unr else ""
        print(f"  {gk:<40} {meta['provider']:<6} {meta['repos']:>4} repos  {out:>4} edges out  "
              f"built {meta['built_at']} ({age_text(meta['built_at'])}){par}{subs}{gaps}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dungeonmap.py", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=None, help=f"cache root (default {CACHE}, DUNGEONMAP_DIR)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build or refresh the graph of one org / group (and its subgroups)")
    b.add_argument("target", help="https://github.com/ORG, gitlab.example.com/GROUP/SUB, ORG (github.com)")
    b.add_argument("--provider", choices=PROVIDERS, help="needed for a self-hosted host")
    b.add_argument("--full", action="store_true", help="rescan every repository, not only changed ones")
    b.add_argument("--no-subgroups", dest="subgroups", action="store_false", help="GitLab: only this group")
    b.add_argument("--jobs", type=int, default=8, help="repositories scanned in parallel (default 8)")
    b.add_argument("--max-files", type=int, default=MAX_FILES,
                   help=f"manifest files read per repository (default {MAX_FILES}); the rest is reported by gaps")
    b.add_argument("--max-tree-entries", type=int, default=MAX_TREE_ENTRIES,
                   help=f"GitLab / Gitea: tree entries listed per repository (default {MAX_TREE_ENTRIES})")
    b.add_argument("--internal", action="append", metavar="NS:PREFIX",
                   help="also treat references under PREFIX as the org's own (npm:@acme-, pypi:acme-, "
                        "maven:com.acme, go:go.acme.dev/); repeatable; replaces the stored list, which "
                        "later builds keep")
    b.add_argument("--timeout", type=int, default=60, help="seconds per API call (default 60)")
    b.add_argument("--save", metavar="DIR", help="also write every API response to DIR/responses.json")
    b.add_argument("--from", dest="from_dir", metavar="DIR", help="replay DIR/responses.json instead of the API")
    sub.add_parser("maps", help="list the graphs in the cache: repos, edges, age, gaps")
    g = sub.add_parser("gaps", help="what the map is missing: repositories read in part, internal "
                                    "references with no repository")
    g.add_argument("names", nargs="*", help="graphs (host/org) or repositories; default all")
    a = ap.parse_args(argv)
    return {"build": cmd_build, "maps": cmd_maps, "gaps": cmd_gaps}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
