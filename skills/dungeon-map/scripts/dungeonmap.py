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

    def tree(self, repo):
        d = self.t.json(f"repos/{repo['path']}/git/trees/{q(repo['default_branch'])}?recursive=1") or {}
        files = [(e["path"], e.get("size")) for e in d.get("tree", []) if e.get("type") == "blob"]
        return files, bool(d.get("truncated"))

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
        try:
            return [g["full_path"] for g in pages(self.t, f"groups/{q(group)}/subgroups")]
        except NotFound:
            return []

    def language(self, repo):
        try:
            langs = self.t.json(f"projects/{repo['_id']}/languages") or {}
        except NotFound:
            return ""
        return max(langs, key=langs.get) if langs else ""

    def tree(self, repo, max_pages=30):
        files, n = [], 0
        for e in pages(self.t, f"projects/{repo['_id']}/repository/tree?recursive=true&ref={q(repo['default_branch'])}",
                       max_pages=max_pages):
            n += 1
            if e.get("type") == "blob":
                files.append((e["path"], None))
        return files, n >= 100 * max_pages

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

    def tree(self, repo, max_pages=10):
        files, total, page = [], None, 1
        while page <= max_pages:
            d = self.t.json(f"repos/{repo['path']}/git/trees/{q(repo['default_branch'])}"
                            f"?recursive=true&per_page=1000&page={page}") or {}
            ents = d.get("tree") or []
            files += [(e["path"], e.get("size")) for e in ents if e.get("type") == "blob"]
            total = d.get("total_count") or 0
            if not ents or not d.get("truncated") and page * 1000 >= total:
                return files, False
            page += 1
        return files, True

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


def _strip_ns(root):
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def x_pom(path, text, ctx):
    try:
        root = _strip_ns(ET.fromstring(text))
    except ET.ParseError:
        return [], []
    prov, req = [], []
    gid = root.findtext("groupId") or root.findtext("parent/groupId") or ""
    aid = root.findtext("artifactId") or ""
    if gid and aid:
        prov.append(("maven", f"{gid}:{aid}".lower()))
    pg, pa = root.findtext("parent/groupId"), root.findtext("parent/artifactId")
    if pg and pa:
        req.append(("maven", "maven", f"{pg}:{pa}".lower(), line_of(text, f"<artifactId>{pa}</artifactId>"), "parent"))
    for dep in root.iter("dependency"):
        g, a = dep.findtext("groupId"), dep.findtext("artifactId")
        if g and a and "${" not in g + a:
            req.append(("maven", "maven", f"{g}:{a}".lower(), line_of(text, f"<artifactId>{a}</artifactId>"),
                        dep.findtext("version") or ""))
    return prov, req


GRADLE_DEP = re.compile(r"""^\s*(?:implementation|api|compileOnly|runtimeOnly|testImplementation|"""
                        r"""testRuntimeOnly|annotationProcessor|kapt|ksp|classpath|compile|testCompile)\b"""
                        r"""\s*\(?\s*['"]([\w.\-]+):([\w.\-]+)(?::[^'"]*)?['"]""")


def x_gradle(path, text, ctx):
    prov, req = [], []
    base = os.path.basename(path)
    if base.startswith("settings.gradle"):
        m = re.search(r"""rootProject\.name\s*=\s*['"]([^'"]+)['"]""", text)
        if m:
            ctx.setdefault("_gradle", {})["root"] = m.group(1)
        return [], []
    m = re.search(r"""^\s*group\s*=?\s*['"]([^'"]+)['"]""", text, re.M)
    if m:
        d = os.path.dirname(path)
        ctx.setdefault("_gradle", {}).setdefault("groups", []).append((d, m.group(1)))
    for i, line in enumerate(text.splitlines(), 1):
        m = GRADLE_DEP.match(line)
        if m:
            req.append(("gradle", "maven", f"{m.group(1)}:{m.group(2)}".lower(), i, line.strip()[:120]))
    return prov, req


def gradle_provides(ctx, repo_name):
    """group:artifact for the root project and each subproject with a build.gradle (dir name)."""
    g = ctx.get("_gradle") or {}
    out = []
    root_group = next((grp for d, grp in g.get("groups", []) if d == ""), None)
    for d, grp in g.get("groups", []):
        art = (g.get("root") or repo_name) if d == "" else os.path.basename(d)
        out.append(("maven", f"{grp}:{art}".lower()))
    if root_group:
        for d in ctx.get("_gradle_dirs", []):
            if d and not any(d == gd for gd, _ in g.get("groups", [])):
                out.append(("maven", f"{root_group}:{os.path.basename(d)}".lower()))
    return out


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


def select_files(files, max_files=80, max_tf=40):
    """Pick the manifest files worth reading from a repository's tree: [(path, extractor)]."""
    skip = re.compile(r"(^|/)(node_modules|vendor|third_party|\.terraform|testdata|fixtures|__fixtures__|_?examples?)/")
    picked, tf = [], 0
    for path, size in sorted(files, key=lambda f: (f[0].count("/"), f[0])):
        if skip.search(path) or (size or 0) > MAX_FILE_BYTES:
            continue
        b = os.path.basename(path)
        bl = b.lower()
        x = None
        if path == ".gitmodules":
            x = x_gitmodules
        elif b == "package.json":
            x = x_package_json
        elif b == "go.mod":
            x = x_go_mod
        elif b == "pyproject.toml":
            x = x_pyproject
        elif b == "setup.cfg":
            x = x_setup_cfg
        elif bl.startswith("requirements") and bl.endswith(".txt"):
            x = x_requirements
        elif b == "pom.xml":
            x = x_pom
        elif b in ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"):
            x = x_gradle
        elif b == "Chart.yaml":
            x = x_chart
        elif b.endswith(".tf") and tf < max_tf:
            x, tf = x_terraform, tf + 1
        elif path == ".gitlab-ci.yml" or (path.startswith(".gitlab/") and bl.endswith((".yml", ".yaml"))):
            x = x_gitlab_ci
        elif re.match(r"^\.(github|gitea)/workflows/[^/]+\.ya?ml$", path) or b in ("action.yml", "action.yaml"):
            x = x_workflow
        elif bl in ("dockerfile", "containerfile") or bl.startswith("dockerfile.") or bl.endswith(".dockerfile"):
            x = x_dockerfile
        if x:
            picked.append((path, x))
    return picked[:max_files]


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
CREATE INDEX provides_ident ON provides(ns, ident);
CREATE INDEX requires_repo ON requires(repo);
"""


def graph_dir(host, org, cache=None):
    safe_host = host.replace(":", "_")
    return os.path.join(cache or CACHE, safe_host, *org.split("/"))


def graph_path(host, org, cache=None):
    return os.path.join(graph_dir(host, org, cache), "map.db")


def scan_repo(prov, repo, max_files):
    """Read a repository's tree and manifest files: (provides, requires, readme, files, note)."""
    host = prov.host
    # identities never carry a port (https and ssh ports differ for the same repository)
    ctx = {"host": host.lower().split(":", 1)[0], "path": repo["path"], "provider": prov.name,
           "git": git_ident(repo["clone_https"]) or norm_path(f"{host}/{repo['path']}")}
    provides = [("git", ctx["git"])] + tf_provides(repo)
    if repo.get("empty") or not repo.get("default_branch"):
        return provides, [], "", [], "empty repository"
    files, truncated = prov.tree(repo)
    note = "tree truncated; deeper files not read" if truncated else ""
    requires, readme, read = [], "", []
    rd = next((p for p, _ in files if "/" not in p and re.match(r"(?i)^readme(\.(md|markdown|rst|txt))?$", p)), None)
    if rd:
        try:
            readme = readme_summary(prov.read(repo, rd))
        except NotFound:
            pass
    picked = select_files(files, max_files)
    ctx["_gradle_dirs"] = [os.path.dirname(p) for p, x in picked if x is x_gradle]
    has_image = False
    for path, x in picked:
        try:
            text = prov.read(repo, path)
        except NotFound:
            continue
        read.append(path)
        p, r = x(path, text, ctx)
        provides += p
        requires += [(via, ns, ident, path, line, detail) for via, ns, ident, line, detail in r]
        has_image = has_image or x is x_dockerfile
    provides += gradle_provides(ctx, repo["name"])
    if has_image:
        provides.append(("image", repo["path"].lower()))
    # drop duplicates, keep the first file:line of each
    seen, req = set(), []
    for row in requires:
        k = row[:3]
        if k not in seen:
            seen.add(k)
            req.append(row)
    return sorted(set(provides)), req, readme, read, note


def build_graph(t, provider, host, org, cache=None, jobs=8, max_files=80, full=False, parent="", log=print):
    """Build (or incrementally refresh) one graph; returns (path, stats, subgroup paths)."""
    prov = PROVIDER_CLASS[provider](t, host)
    t0 = time.time()
    repos = prov.list_repos(org)
    subs = prov.subgroups(org)
    dbp = graph_path(host, org, cache)
    old = {}
    if os.path.exists(dbp) and not full:
        try:
            odb = sqlite3.connect(dbp)
            if dict(odb.execute("SELECT key, value FROM meta")).get("schema") == str(SCHEMA):
                for row in odb.execute("SELECT path, last_activity, default_branch, note FROM repos"):
                    old[row[0]] = row[1:]
                old["__db__"] = odb
        except sqlite3.Error:
            old = {}
    stats = {"repos": len(repos), "scanned": 0, "reused": 0, "failed": 0, "requires": 0}
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
            return r, scan_repo(prov, r, max_files)
        except ApiError as e:
            if isinstance(e, AuthError):
                raise
            return r, ([("git", git_ident(r["clone_https"]) or "")], [], "", [], f"error: {e}"[:300])

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
            db.executemany("INSERT INTO provides VALUES (?,?,?)", odb.execute("SELECT * FROM provides WHERE repo=?", (path,)))
            db.executemany("INSERT INTO requires VALUES (?,?,?,?,?,?,?)", odb.execute("SELECT * FROM requires WHERE repo=?", (path,)))
            stats["reused"] += 1
        else:
            provides, requires, readme, files, note = res
            files, scanned = json.dumps(files), stamp
            db.executemany("INSERT INTO provides VALUES (?,?,?)", [(path, ns, i) for ns, i in provides if i])
            db.executemany("INSERT INTO requires VALUES (?,?,?,?,?,?,?)", [(path,) + row for row in requires])
            stats["failed" if note.startswith("error") else "scanned"] += 1
        db.execute("INSERT INTO repos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            path, r["name"], r["description"], json.dumps(sorted(r.get("topics") or [])), r["language"],
            r["default_branch"], int(r["archived"]), int(r["fork"]), r["last_activity"], r["web_url"],
            r["clone_https"], r["clone_ssh"], readme, files, note, scanned))
    stats["requires"] = db.execute("SELECT COUNT(*) FROM requires").fetchone()[0]
    db.executemany("INSERT INTO subgroups VALUES (?)", [(s,) for s in subs])
    meta = {"schema": str(SCHEMA), "provider": provider, "host": host, "org": org, "parent": parent,
            "built_at": stamp, "seconds": f"{time.time() - t0:.1f}", "api_calls": str(t.calls)}
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


def cmd_build(a):
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
                                        full=a.full, parent=parent)
            built.append(o)
            print(f"{host}/{o}: {st['repos']} repos ({st['scanned']} scanned, {st['reused']} unchanged, "
                  f"{st['failed']} failed), {st['requires']} references -> {dbp}")
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
    n_edges = sum(1 for e in m.edges if e["src_graph"] in {f"{host}/{o}" for o in built})
    print(f"map: {len(m.graphs)} graphs, {len(m.repos)} repos, {len(m.edges)} cross-repository edges "
          f"({n_edges} from what was just built)")
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

    def out_edges(self, key):
        return [e for e in self.edges if e["src"] == key]

    def in_edges(self, key):
        return [e for e in self.edges if e["dst"] == key]

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
    for src, via, ns, ident, f, line, detail, gk in requires:
        dsts, how = lookup(ns, ident)
        for dst in sorted(dsts):
            if dst == src or (src, dst, via) in seen:
                continue
            seen.add((src, dst, via))
            m.edges.append({"src": src, "dst": dst, "via": via, "ns": ns, "ident": ident, "file": f,
                            "line": line, "detail": detail, "match": how, "src_graph": gk})
    return m


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
        print(f"  {gk:<40} {meta['provider']:<6} {meta['repos']:>4} repos  {out:>4} edges out  "
              f"built {meta['built_at']} ({age_text(meta['built_at'])}){par}{subs}")
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
    b.add_argument("--max-files", type=int, default=80, help="manifest files read per repository (default 80)")
    b.add_argument("--timeout", type=int, default=60, help="seconds per API call (default 60)")
    b.add_argument("--save", metavar="DIR", help="also write every API response to DIR/responses.json")
    b.add_argument("--from", dest="from_dir", metavar="DIR", help="replay DIR/responses.json instead of the API")
    sub.add_parser("maps", help="list the graphs in the cache: repos, edges, age")
    a = ap.parse_args(argv)
    return {"build": cmd_build, "maps": cmd_maps}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
