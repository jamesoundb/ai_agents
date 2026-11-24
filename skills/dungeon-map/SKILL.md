---
name: dungeon-map
description: >
  Use to build or refresh a map of every repository in a GitHub org, GitLab group or Gitea org
  (metadata, README summary, cross-repository dependencies) through gh / glab / gt, without
  cloning; compass reads it to pick the repositories a problem needs.
allowed-tools: Bash(*/dungeon-map/scripts/run.sh *), Bash(gh auth status*), Bash(glab auth status*), Bash(gt auth status*), Read
---

# dungeon-map: one graph per org, linked into one map

`scripts/run.sh build <org>` lists the org's repositories through the provider's own CLI
(`gh api`, `glab api`, `gt api`), reads each repository's tree and a few manifest files through
the API (nothing is cloned), and writes one SQLite graph per org, and one per GitLab subgroup, to
`~/.cache/dungeon-map/<host>/<org path>/map.db`. Together the cached graphs are the map: an edge
between two orgs, or two providers, appears as soon as both are built.

Per repository: name, path, description, topics, primary language, default branch, archived and
fork flags, last activity, a README summary, and the repositories it depends on, each with
file:line: git submodules, npm / Go / Python dependencies, Maven (parents, BOMs, properties),
Gradle (version catalogs, convention plugins, plugin ids, composite builds), sbt, Clojure, .NET
(PackageReference, ProjectReference to sibling repositories, packages.config, paket), Helm chart
dependencies, Terraform module sources (git URLs and registry names), GitLab CI `include:` /
`trigger:` / components, GitHub and Gitea workflow and action `uses:`, Dockerfile `FROM` images
built in the org.

Engine: `scripts/run.sh` (stdlib Python 3.10+). Call it by its full path, one command per call,
nothing piped or chained after it. It never sees a token: the CLI holds the login, and the map
holds no credentials.

## Workflow

1. **Logged in?** `gh auth status`, `glab auth status`, `gt auth status`. Not logged in: ask the
   developer to sign in through the browser (do not ask for a token, do not export one):
   - GitHub: `gh auth login` (GitHub Enterprise: `--hostname HOST`)
   - GitLab: `glab auth login` (self-hosted: `--hostname HOST`)
   - Gitea: `gt auth login --host HOST --oauth --scopes read:repository,read:user,read:organization`
     (an org's repository list needs `read:organization`)

   A `GITHUB_TOKEN` / `GH_TOKEN` / `GITLAB_TOKEN` in the environment overrides the CLI's stored
   login; if a build fails with 401/403 while `auth status` looks fine, that variable is the cause.
2. **Build once per org or top-level group**:

   | Org | One call |
   |---|---|
   | GitHub org or user | `run.sh build https://github.com/ORG` |
   | GitLab group (subgroups included) | `run.sh build https://gitlab.com/GROUP` |
   | self-hosted GitLab / Gitea / GitHub Enterprise | `run.sh build HOST/ORG --provider gitlab\|gitea\|github` |

   A 100-repository org takes about a minute. Rebuilding is incremental: repositories whose last
   activity and default branch are unchanged keep their rows (`--full` rescans all; a newer
   scanner version or other limits rescan all by themselves). Run the same command to refresh a
   stale map.
3. **Check it**: `run.sh maps` lists every cached graph with repositories, edges, age and gaps.
   When it shows gaps, `run.sh gaps` lists them; tell the developer, since the map is missing
   exactly what they list. Then hand over to the `compass` skill to find repositories and clone
   them.

## Reading the output

- `N repos (S scanned, U unchanged, F failed, I incomplete)`: a failed repository keeps its
  metadata and has `note: error ...` (compass marks it `not scanned`); rebuilding retries it.
  `incomplete`: repositories the scan read only in part (see `gaps`).
- `rate limited by HOST; waiting Ns`: the build pauses and continues by itself; let it run
  (gitlab.com's free plan: about 8 repositories a minute on a first build).
- `gaps: R repositories read incompletely, N internal references with no repository`: run
  `run.sh gaps [GRAPH|REPO...]`. It lists each repository read in part, and why:
  - `read 300 of 707 manifests`: raise `--max-files`;
  - `tree listing stopped` / `GitHub truncated the tree listing`;
  - `manifest(s) over 512 KB not read`;
  - `scan failed`.
  It also lists every reference in the org's own namespaces (its host/org path, npm scope, Maven
  group, Go vanity domain, Terraform namespace) that no repository in the map provides, with
  file:line. That is usually a repository the map is missing: private, in
  another group not built yet, or renamed. Add namespaces the map can't infer with
  `build --internal NS:PREFIX`. `gaps` suggests one when the org's PyPI or npm packages share its
  name (`opentelemetry-*`). Ask the developer before adding it: anyone can publish under such a
  name.
- Edges come only from files the map read (up to 300 manifests per repository, `--max-files`;
  `node_modules/`, `vendor/`, `testdata/`, `fixtures/`, `examples/` skipped). A public package or
  image that no repository of a cached org provides is neither an edge nor a gap.

Options, the file types and identities per edge, matching rules, the cache layout and the test
setup (a throwaway Gitea in Docker): `reference/usage.md`.
