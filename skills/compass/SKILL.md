---
name: compass
description: >
  Use when a problem needs repositories the workspace does not have yet, to set up or verify the
  build / test environment of an application (every repository it needs, at any depth), or to find
  which repositories in the org do X, depend on Y or are used by Z: queries the dungeon map and
  hands off to ai-task clone / start.
allowed-tools: Bash(*/compass/scripts/run.sh *), Bash(.ai/bin/ai-task *), Read
---

# compass: from a problem to the repositories it needs

`scripts/run.sh` queries the map that the `dungeon-map` skill builds
(`~/.cache/dungeon-map/`, every org and group that was built). It never calls a provider API, so
it answers in well under a second. It knows the ai-task workspace (the nearest parent with
`.ai/`): what is already cloned, and the commands that clone the rest and start the task.

Engine: `scripts/run.sh` (stdlib Python 3.10+, reads `../dungeon-map/scripts/dungeonmap.py`).
Call it by its full path, one command per call, nothing piped or chained after it.

## Workflow

1. **Ask in one call**:

   | You need | One call |
   |---|---|
   | the repositories a problem needs, and the next commands | `plan "the problem in its own words"` (`--slug S`) |
   | repositories by keyword, topic or language | `find WORDS` (`--topic T`, `--language L`) |
   | one repository: metadata, what it depends on, what uses it | `show REPO` (several at once) |
   | everything an application's build / test environment needs, at any depth | `deps APP --kinds build,ci` (`--handoff --slug S`, `--check`) |
   | clone commands for repositories you already chose | `handoff REPO... --slug S` |
   | which maps exist and how old they are | `status` |

   REPO is a path (`acme/payments-api`), a bare name when it is unique, or a URL.
2. **Judge the proposal.** `plan` proposes up to 3 repositories (`--max`) whose name, topics,
   description or README match the problem's words, with the matched words as the reason, and lists
   their direct dependencies and dependents as `related`, each with the file:line that links them.
   Add a related repository only when the change crosses into it (an interface, a shared type, a
   version bump); otherwise leave it out. When the developer's words do not match, `find` with
   other words or ask which repository they mean. Do not clone by guessing.
3. **Build or test environment of an application** ("set up everything I need to build A",
   "why does my local build of A miss something"): `deps A`, not `plan`. A code change needs the
   repositories it touches; an environment needs all of them.
   - Pick the kinds:
     - `build`: package managers, submodules, Terraform modules, base images;
     - `ci`: includes, components, actions, reusable workflows;
     - `deploy`: Helm charts.
     A local build is `--kinds build`; a CI-like run adds `ci`. Leave the flag off for everything.
   - Every repository comes with the chain of evidence that pulls it in. A cycle is listed once
     under `cycles`.
   - **Read the verdict and report it.** `verdict: incomplete` lists every gap on the way:
     - repositories read in part;
     - internal references no repository provides;
     - groups the map doesn't have, each with the `dungeon-map build` command that adds it;
     - archived dependencies;
     - `--depth` cut-offs;
     - stale maps.
     Never tell the developer the environment is complete while the verdict says `incomplete`:
     name each gap. Build the missing groups first when the developer can reach them, then run
     `deps` again.
   - `--check` in an existing workspace lists the closure repositories it does not have (exit 1).
     `--handoff --slug S` prints the commands for the whole closure; hand off as in step 4.
4. **Hand off.** Run the `next` commands as printed, one per call, from the workspace root:
   `.ai/bin/ai-task clone <spec>` for each missing repository (owner/name on github.com, the full
   URL elsewhere), then `run.sh seed REPO...` (writes the map's facts into
   `.ai/instructions/reference/<repo>.md`, between markers, next to the developer's own notes),
   then `.ai/bin/ai-task start <slug> <repos...>`. The problem file `.ai/instructions/<slug>.md`
   must exist before `start`; write it first when the problem came from chat.

## Reading the output

- `have`: already cloned (origin matches); `missing`: to clone; `CLASH`: a clone with that
  directory name holds another repository. ai-task names clones by basename, so tell the
  developer; do not clone it under the same name.
- `[archived]`, `[fork]`, `[not scanned]`, `[read incompletely]` after a name; archived
  repositories are never proposed. `show` lists a repository's `gap:` lines and its
  `internal references with no repository in the map`. Tell the developer both: those are the
  dependencies the map can't name a repository for (`dungeon-map gaps` lists them for the whole
  org).
- `name match` on an edge: the dependency was matched by path prefix (a Go package inside a module,
  an image under a project path), not exactly.
- `no dungeon map` or `stale: ... built N days ago`: build or refresh it with the printed
  `dungeon-map` command (the `dungeon-map` skill) before trusting the answer; a stale map misses
  new repositories and dependencies.
