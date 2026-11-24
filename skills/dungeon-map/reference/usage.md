# dungeon-map usage reference

## Build

```bash
scripts/run.sh build TARGET [--provider github|gitlab|gitea] [--full] [--no-subgroups]
                            [--jobs 8] [--max-files 300] [--max-tree-entries 20000]
                            [--internal NS:PREFIX ...] [--timeout 60] [--save DIR] [--from DIR]
scripts/run.sh maps
scripts/run.sh gaps [GRAPH|REPO ...]
```

All commands take `--cache DIR` before the command name (default `~/.cache/dungeon-map`,
`DUNGEONMAP_DIR`).

- TARGET: `https://github.com/ORG`, `github.com/ORG`, `ORG` (github.com), `gitlab.com/GROUP/SUB`,
  `HOST/ORG`. github.com and gitlab.com need no `--provider`, any other host does (Gitea, a
  self-hosted GitLab, GitHub Enterprise; gitea.com is assumed Gitea).
- GitHub: an org, else the user account of that name. GitLab: the group, then each subgroup as
  its own graph (`--no-subgroups` builds only the named group); a user namespace works too. Gitea:
  an org, else a user.
- Repositories are scanned 8 at a time (`--jobs`). Per repository: the recursive tree (one call on
  GitHub; GitLab pages of 100 entries and Gitea pages of the server's size, up to
  `--max-tree-entries`, 20,000), the root README, and up to 300 manifest files (`--max-files`,
  one call each). Files over 512 KB are not read. GitLab costs one more call per repository for its
  languages. Older Gitea leaves topics out of the listing: one call per repository then.
- Under the cap, manifests are chosen by kind, not by depth alone. The first manifest of each kind
  in a directory comes before a second one of that kind there, and the kinds take turns, so 700
  `build.gradle` files can't crowd out a repository's Dockerfiles and workflows.
- Cost of the cap, measured on open-telemetry's 113 repositories (2026-11-24, from the saved trees):

  | `--max-files` | repositories over it | manifest reads |
  |---|---|---|
  | 80 (before) | 13 | 3,022 |
  | 300 (default) | 3 (707, 409, 308 manifests) | 4,032 |
  | 500 | 1 | 4,349 |
  | unlimited | 0 | ~4,560 |

  A full read costs about as many calls as GitHub allows in an hour (5,000), so the default stops
  at 300 and reports the rest.
- Gaps: every limit that leaves a scan incomplete is stored per repository and listed by `gaps`,
  `maps` (counts) and compass `show`:

  | kind | when |
  |---|---|
  | `manifest-cap` | more manifests than `--max-files` (found and read counts) |
  | `tree-truncated` | GitHub truncated the recursive tree (over 100,000 entries or 7 MB), or the GitLab / Gitea listing reached `--max-tree-entries` |
  | `file-too-large` | manifests over 512 KB, by path |
  | `scan-failed` | the repository's scan failed (also `note: error ...`) |

- Internal references with no repository are computed when the map loads, across every cached
  graph. A reference counts as the org's own when it is under one of these, taken from what the
  org's non-fork repositories provide (a fork carries its upstream's names):
  - a graph's `host/org/` path (`git`, `go`);
  - the org path as an image path (`acme/...`);
  - a Go vanity domain (`go.acme.dev/`);
  - an npm scope (`@acme/`);
  - a Maven groupId and its children (`com.acme`, `com.acme.billing`);
  - a Terraform registry namespace.

  A shared leading name is not a namespace. On PyPI and unscoped npm anyone can publish
  `opentelemetry-instrumentation-foo`, and Traceloop publishes 34 such packages that
  open-telemetry's own repositories depend on, so it's only suggested. When two or more of the
  org's packages start with the org's name, `gaps` prints `note: ... rebuild with --internal
  pypi:opentelemetry-`. `--internal NS:PREFIX` adds a namespace (NS: git, go, npm, pypi, maven,
  image, tfreg, helm). The list is stored in the map, later builds keep it, and passing
  `--internal` again replaces it. Public packages outside these namespaces are never reported.
- Incremental: a repository whose last activity (GitHub `pushed_at`, GitLab `last_activity_at`,
  Gitea `updated_at`) and default branch match the previous build keeps its rows. Repositories
  that failed last time are always rescanned. `--full` rescans everything. So does a build whose
  scanner version (`SCANNER` in `dungeonmap.py`, `meta.scanner`), `--max-files` or
  `--max-tree-entries` differs from the last build's, which prints the reason. **Bump `SCANNER`
  whenever an extractor or the file selection changes**, or the fix never reaches repositories
  that have not changed. Gitea sets `updated_at`
  a few seconds after a push (asynchronously), so a push made while a build is listing the org is
  picked up by the next build.
- The graph is written to a temporary file and renamed into place, so a failed build leaves the
  previous graph intact.
- `--save DIR` writes every API response to `DIR/responses.json` (for tests, or to share an org's
  map with someone without access); `--from DIR` builds from such a file without any API call.

- Rate limits: an answer of HTTP 429, or a 403 that says "rate limit" (GitHub's secondary limits),
  pauses every thread for 15, 30, 60, 60, 60 s in turn (`rate limited by HOST; waiting Ns` on
  stderr) and then the call is retried; after the last wait the call fails. gitlab.com's free plan allows
  about 100 calls a minute per user and a repository costs about 12, so a large group there builds at
  roughly 8 repositories a minute; incremental rebuilds cost far less.

Exit codes: 0 built, 2 API error or org not found, 3 not logged in (the message names the login
command).

## Transport

| provider | command | raw files |
|---|---|---|
| GitHub | `gh api [--hostname H] ENDPOINT` | `-H "Accept: application/vnd.github.raw"` on `repos/O/R/contents/PATH?ref=B` |
| GitLab | `glab api [--hostname H] ENDPOINT` | `projects/ID/repository/files/PATH/raw?ref=B` |
| Gitea | `gt --no-input --host H api ENDPOINT` | `repos/O/R/raw/PATH?ref=B` |

Query parameters are part of the endpoint and pages are requested with `page=` (not the CLIs'
`--paginate`, whose output differs per CLI; `glab api -F` would turn the request into a POST).
A page shorter than `per_page` ends the list only once a full page proved the server honours
`per_page`, so a server whose page cap is lower (Gitea `MAX_RESPONSE_ITEMS`) is still read to the end.

## What is read, what it means

| file | provides | requires (via) |
|---|---|---|
| every repository | `git` host/path of its clone URL | |
| `.gitmodules` (root) | | `git` each `url =`, relative URLs resolved against the repository (submodule) |
| `package.json` | `npm` name | `npm` each dependency (all four sections); `git` for git / `github:` / `gitlab:` versions |
| `go.mod` | `go` module path | `go` each `require` |
| `pyproject.toml`, `setup.cfg`, `requirements*.txt` | `pypi` name (PEP 503 normalized) | `pypi` each dependency; `git` for `x @ git+...` and `git+` URLs (python) |
| `pom.xml` | `maven` groupId:artifactId | `maven` parent and each dependency |
| `build.gradle[.kts]`, `settings.gradle[.kts]` | `maven` group:rootProject.name, group:subdir per subproject | `maven` each `implementation 'g:a:v'`-style string (gradle) |
| `Chart.yaml` | `helm` chart name | `helm` each dependency not from `file://` |
| `*.tf` | `tfreg` owner/name/provider for a repository named `terraform-<provider>-<name>` | `git` for git / `github.com/...` sources, `tfreg` for registry sources (terraform) |
| `.gitlab-ci.yml`, `.gitlab/**/*.yml` | | `git` for `include: project:` and `remote: .../-/raw/...` (ci-include), `trigger:` (ci-trigger), `component:` (ci-component); `$CI_PROJECT_NAMESPACE`, `$CI_SERVER_HOST`/`FQDN` expanded, other variables skipped |
| `.github/workflows/*`, `.gitea/workflows/*`, `action.yml` | | `git` for `uses: o/r/.github/workflows/x.yml@v` (workflow) and `uses: o/r[/path]@v` (action), on the repository's own host; full URLs as written; `docker://` as `image` |
| `Dockerfile*`, `*.dockerfile`, `Containerfile` | `image` the repository's path | `image` each `FROM` without registry host and tag; `ARG` defaults substituted; stage aliases, `scratch` and unresolved variables skipped |

Identities are lowercase and carry no port (`localhost:3300/acme/x` and `git@localhost:acme/x`
are the same repository). Skipped directories: `node_modules`, `vendor`, `third_party`,
`.terraform`, `testdata`, `fixtures`, `__fixtures__`, and example code (`example`, `examples`,
`_example`, `_examples`), whose dependencies are not the repository's own.

## Matching (at load, across every cached graph)

- Exact identity first.
- `git`, `go` and `image` then try the longest path prefix (a Go package inside a module, a
  GitLab image `group/project/sub`, a GitHub URL with extra path); a `go` requirement also matches
  a repository's `git` identity. These edges are marked `prefix` (compass: `name match`).
- A renamed repository has a new identity: references to its old path (a `FROM` image, a
  submodule URL) stop matching until their files are changed, even where the provider redirects.
- An edge to the repository itself is dropped; each (from, to, via) appears once, with the first
  file:line that produced it.
- Package-name matches (`npm`, `pypi`, `maven`, `helm`, `tfreg`) are by name only: a public package
  of the same name as an org repository's package would match. The detail column keeps the
  version / repository URL so this can be checked.

## Cache layout

```
~/.cache/dungeon-map/
  github.com/acme/map.db
  gitlab.example.com/platform/map.db              # group
  gitlab.example.com/platform/ci/map.db           # subgroup: meta.parent = platform
  localhost_3300/dmtest/map.db                    # ':' in a host becomes '_'
```

Tables: `meta` (provider, host, org, parent, built_at, api_calls, schema, scanner, max_files,
max_tree_entries, internal), `repos`, `provides` (repo, ns, ident), `requires` (repo, via, ns,
ident, file, line, detail), `gaps` (repo, kind, found, read, detail), `subgroups`. Each user
builds their own copy; nothing in it is a secret, but it can describe private repositories.

## Tests

```bash
python3 skills/dungeon-map/tests/test_dungeonmap.py   # offline: Gitea replay + GitHub/GitLab API shapes
python3 skills/compass/tests/test_compass.py          # offline: compass on the same fixture
DM_GITEA_TREE_PAGE=5 skills/dungeon-map/tests/gitea.sh up  # live: throwaway Gitea in Docker, org dmtest
skills/dungeon-map/tests/gitea.sh relogin read:repository,read:user,read:organization  # the documented scopes only
skills/dungeon-map/scripts/run.sh build localhost:3300/dmtest --provider gitea --save DIR
skills/dungeon-map/tests/gitea.sh down                # logs gt out, removes the container
```

`tests/org/` is the test org: 14 repositories with one planted edge of each kind and decoys that
must not match (public packages, public base images, a public registry module). It also holds four
internal references with no repository (an npm scope, a `dmtest-*` PyPI name, a Go module under
host/org, a Terraform namespace) and their look-alike decoys. `dmtest-metrics` is reported only
with `--internal pypi:dmtest-`. `big-monorepo` hits every limit:
6 npm and 2 PyPI manifests (`--max-files 4`), a 21-entry tree (`--max-tree-entries 5`), and a
600 KB `package.json` that `seed_gitea.py` generates (`oversized` in `repos.json`). `gitea.sh up`
pipes an admin token from `gitea admin` straight into `gt auth login --with-token`; it is never in
an environment variable or a file. `tests/fixture/gitea/responses.json` is a `--save` of that org
with the default limits, recorded with `DM_GITEA_TREE_PAGE=5` so trees take several pages.
`DM_GITEA_MAX_ITEMS=5 DM_GITEA_TREE_PAGE=3 tests/gitea.sh up` lowers the server's page caps so the
listings and trees of the small org need several pages; `DM_GITEA_IMAGE=gitea/gitea:1.20` (the oldest
the seeder supports) leaves topics out of the listing, which exercises the per-repository topics call.
