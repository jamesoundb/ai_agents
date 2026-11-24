---
name: compass
description: >
  Use when a problem needs repositories the workspace does not have yet, or to find which
  repositories in the org do X, depend on Y or are used by Z: queries the dungeon map, proposes
  the minimal set to clone with a reason each, and hands off to ai-task clone / start.
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
   | clone commands for repositories you already chose | `handoff REPO... --slug S` |
   | which maps exist and how old they are | `status` |

   REPO is a path (`acme/payments-api`), a bare name when it is unique, or a URL.
2. **Judge the proposal.** `plan` proposes up to 3 repositories (`--max`) whose name, topics,
   description or README match the problem's words, with the matched words as the reason, and lists
   their direct dependencies and dependents as `related`, each with the file:line that links them.
   Add a related repository only when the change crosses into it (an interface, a shared type, a
   version bump); otherwise leave it out. When the developer's words do not match, `find` with
   other words or ask which repository they mean. Do not clone by guessing.
3. **Hand off.** Run the `next` commands as printed, one per call, from the workspace root:
   `.ai/bin/ai-task clone <spec>` for each missing repository (owner/name on github.com, the full
   URL elsewhere), then `run.sh seed REPO...` (writes the map's facts into
   `.ai/instructions/reference/<repo>.md`, between markers, next to the developer's own notes),
   then `.ai/bin/ai-task start <slug> <repos...>`. The problem file `.ai/instructions/<slug>.md`
   must exist before `start`; write it first when the problem came from chat.

## Reading the output

- `have`: already cloned (origin matches); `missing`: to clone; `CLASH`: a clone with that
  directory name holds another repository. ai-task names clones by basename, so tell the
  developer; do not clone it under the same name.
- `[archived]`, `[fork]`, `[not scanned]` after a name; archived repositories are never proposed.
- `name match` on an edge: the dependency was matched by path prefix (a Go package inside a module,
  an image under a project path), not exactly.
- `no dungeon map` or `stale: ... built N days ago`: build or refresh it with the printed
  `dungeon-map` command (the `dungeon-map` skill) before trusting the answer; a stale map misses
  new repositories and dependencies.
