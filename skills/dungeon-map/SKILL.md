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
file:line: git submodules, npm / Go / Python / Maven / Gradle dependencies, Helm chart
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
   activity and default branch are unchanged keep their rows (`--full` rescans all). Run the same
   command to refresh a stale map.
3. **Check it**: `run.sh maps` lists every cached graph with repositories, edges and age. Then
   hand over to the `compass` skill to find repositories and clone them.

## Reading the output

- `N repos (S scanned, U unchanged, F failed)`: a failed repository keeps its metadata and has
  `note: error ...` (compass marks it `not scanned`); rebuilding retries it.
- `rate limited by HOST; waiting Ns`: the build pauses and continues by itself; let it run
  (gitlab.com's free plan: about 8 repositories a minute on a first build).
- `tree truncated`: a very large repository; files deeper than the API returned were not read.
- Edges come only from files the map read (at most 80 manifests per repository, `--max-files`;
  `node_modules/`, `vendor/`, `testdata/`, `fixtures/`, `examples/` skipped). A dependency on a package or
  image no repository of a cached org provides is not an edge.

Options, the file types and identities per edge, matching rules, the cache layout and the test
setup (a throwaway Gitea in Docker): `reference/usage.md`.
