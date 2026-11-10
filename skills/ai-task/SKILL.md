---
name: ai-task
description: >
  Use when asked to set up a workspace of cloned repositories, or to start, track, resume or close a
  task in one: git worktrees on a feature branch, a problem definition and a worklog per problem.
allowed-tools: Bash(*/ai-task/scripts/ai-task *), Bash(.ai/bin/ai-task *), Bash(*/.ai/bin/ai-task *), Bash(git *), Read
---

# ai-task: one-command task workspaces

A workspace is a directory of clones plus `.ai/`. Each problem gets a problem definition, a worklog
and disposable worktrees, so the clones stay on the default branch.

```
<workspace>/
  repo_a/, repo_b/                     clones; never edited, only fast-forwarded
  .tasks/<id>/<repo>/                  worktrees on feature/<slug>; all changes go here
  .ai/                                 its own git repository
    bin/ai-task                        the copy sessions run
    instructions/<slug>.md             the problem definition, usually written first
    instructions/reference/<x>.md      lasting notes per repository or topic
    instructions/archive/<id>.md       problem definitions of closed tasks
    ai_work_logs/<id>.md, INDEX.md     one worklog per problem; a generated index
```

## How to run it

Start sessions at the workspace root. A sandbox can reach only the session directory, and a
worktree's git data lives in the main clone, so a sandboxed session started in `.tasks/<id>/` cannot
commit.

From the root, call `.ai/bin/ai-task <command>`, one command per call: no `cd`, no pipes, no chains.
Permission rules match the literal command.

**No workspace yet:** run this skill's `scripts/ai-task init` by its installed path (for example
`~/.claude/skills/ai-task/scripts/ai-task init`) in the directory that holds the clones. Existing
clones are not changed. Rerun `init` after updating this skill to refresh `.ai/bin/ai-task`.

## Procedure

1. Read `.ai/instructions/<slug>.md`. If the user described the problem in chat, write it there
   first. The filename is the slug: lowercase letters, digits and hyphens.
2. `.ai/bin/ai-task start <slug> [repo...] [--title "text"]`. List the repositories the problem
   needs, or none. Rerun with the same slug to add one. `.ai/bin/ai-task clone <owner/name>` adds a
   repository that is missing.
3. Work in `.tasks/<id>/<repo>/`. At each milestone run
   `.ai/bin/ai-task note <slug> "what was found, decided, the result"`, which records the real time.
4. Commit and push only when asked.
5. Add a final `note` with the result, then `.ai/bin/ai-task close <slug>`. It refuses when work is
   uncommitted or unpushed or the worklog is empty. Report that; use `--force` only if the user asks.

`.ai/bin/ai-task status` shows open tasks and unstarted instructions. `.ai/bin/ai-task list [repo]`
shows history. `.ai/bin/ai-task sync` updates every clone it safely can.

## Notes

- `sync` and `start` carry on with the last fetched state when a sandbox blocks the network.
  `clone` needs the network.
- Claude Code permission rules need both forms, since agents also call the absolute path:
  `Bash(.ai/bin/ai-task *)` and `Bash(*/.ai/bin/ai-task *)`.
- Antigravity needs a grant for the command in headless mode: `command(.ai/bin/ai-task)`.
- Gemini CLI's Docker sandbox mounts only the launch directory and replaces `HOME`, so it does not
  see `~/.gemini/GEMINI.md` or installed skills. `start`, `init` and `clone` copy the git identity
  into each clone so that commits still work there.
