# ai-task: a workspace, a problem file and a worklog for every task

`ai-task` gives AI-assisted work a fixed shape. You clone your repositories once into one folder,
the **workspace**. For each problem you write a short **problem file**. The agent then creates a
git worktree on `feature/<slug>` for each repository the problem needs, and keeps a timestamped
**worklog** of what it found, decided and changed. When the work is pushed, `close` removes the
worktrees and archives the problem file. The clones themselves stay on the default branch and are
only ever fast-forwarded.

It works the same without repositories: a folder such as `~/wifi_troubleshooting` becomes a
workspace with a problem file and a worklog, and no worktrees.

`SKILL.md` in this folder is what the agent reads. This README is for people.

## Layout

```
~/dev/                                 the workspace: any folder, any name
  repo_a/  repo_b/                     clones on the default branch; never edited, only fast-forwarded
  .tasks/<id>/<repo>/                  worktrees on feature/<slug>: all changes go here
  .ai/                                 its own git repository: the history of every task
    bin/ai-task                        the copy sessions run
    instructions/<slug>.md             the problem definition, written before the session
    instructions/reference/<repo>.md   lasting notes per repository or topic (not a worklog)
    instructions/archive/<id>.md       problem definitions of closed tasks
    ai_work_logs/<id>.md               one worklog per problem, with tracking front matter
    ai_work_logs/INDEX.md              generated index of every task
```

`<slug>` is the problem's name in lowercase letters, digits and hyphens (`fix-login-timeout`).
`<id>` is the slug with its start date: `2026-11-12-fix-login-timeout`.

## Set up once per machine

1. **Install the skill** with the rest of this repository's skills, for each tool you use:

   ```bash
   git clone <this repository> ~/ai_agents
   ~/ai_agents/install.sh --harness claude,antigravity --scope user
   ```

   Where it lands: `~/.claude/skills/ai-task` (Claude Code), `~/.gemini/config/skills/ai-task`
   (Antigravity), `~/.agents/skills/ai-task` (Gemini CLI, Codex).

2. **Put `ai-task` on your PATH.** The install above also links `~/.local/bin/ai-task` to the
   script in your clone, so you can type `ai-task init` in any folder. If `~/.local/bin` is not on
   your PATH yet, the installer says so and prints the line to add; it never edits your shell
   files itself. Add it to `~/.zshrc` (macOS) or `~/.bashrc` (Linux), then open a new terminal:

   ```bash
   export PATH="$HOME/.local/bin:$PATH"
   ```

   Many Linux distributions already put `~/.local/bin` on PATH at login once the folder exists.
   Check with `command -v ai-task`. Details below, under "The `ai-task` command".

3. **Give the agent the workflow rules.** The agent follows this workflow because its global
   instructions tell it to. The [global_ai_instructions](https://gitlab.com/jamesoundb/global_ai_instructions)
   repository has a ready-made `CLAUDE.md` and `GEMINI.md` with a "Workspace workflow" section, and
   an `install.sh` that links them into `~/.claude/` and `~/.gemini/`. Read them before installing:
   they replace your current global files (which are kept as `.bak-<timestamp>`). To keep your own,
   copy just the "Workspace workflow" section into them.

4. **Optional, Claude Code: stop the permission prompts.** Add both forms to `permissions.allow` in
   `~/.claude/settings.json`; agents call the script by its relative and its absolute path:

   ```json
   "Bash(.ai/bin/ai-task *)", "Bash(*/.ai/bin/ai-task *)"
   ```

   Antigravity in headless mode needs the grant `command(.ai/bin/ai-task)`.

## Create a workspace

Run `init` once, in the folder that holds (or will hold) your clones. Existing clones are left as
they are:

```bash
cd ~/dev
ai-task init
# without the PATH entry: ~/.claude/skills/ai-task/scripts/ai-task init (or your tool's path, above)
```

`init` refuses inside another workspace and inside a git checkout. Do not run it in your home
directory: every folder below would belong to that workspace. Add repositories with `clone`, which
accepts a full URL or, for GitHub, `owner/name`:

```bash
ai-task clone https://gitlab.example.com/team/service.git
ai-task clone owner/name                    # short form: github.com only
```

## Day to day

1. **Write the problem** in `.ai/instructions/<slug>.md`: what is wrong, what done looks like, and
   which repositories are involved. Or skip this and describe the problem in chat; the agent writes
   the file for you.
2. **Start the session at the workspace root**, not in a repository or a task folder.
3. **Ask for the work in plain words.** The agent reads the problem file and runs
   `.ai/bin/ai-task start <slug> <repos>`. As it works it records each milestone with `note`, and it
   commits and pushes only when you ask.
4. **Close** when the work is pushed: the agent adds a final note and runs `close`.

Everything the agent does is a plain command, so you can run any of them yourself:

| Command | What it does |
|---|---|
| `ai-task init` | Make the current folder a workspace. Rerun after updating the skill, to refresh `.ai/bin/ai-task`. |
| `ai-task clone <url\|owner/name>...` | Clone into the workspace once and create its reference file. |
| `ai-task sync [repo...]` | Fetch every clone; fast-forward those that are clean and on the default branch. Others are only reported. |
| `ai-task start <slug> [repo...] [--title "text"]` | Use `.ai/instructions/<slug>.md` (a stub if there is none), sync the repositories, create worktrees on `feature/<slug>` and the worklog. Rerun with another repository to add it to the task. |
| `ai-task note <slug> "text"` | Add a worklog entry stamped with the real time. |
| `ai-task status` | Open tasks with uncommitted and unpushed counts, and problem files not started yet. |
| `ai-task list [repo]` | Every task, newest first; optionally only those touching a repository. |
| `ai-task close <slug> [--force]` | Remove the worktrees, archive the problem file, mark the worklog done. |

You type `ai-task` (the command on your PATH). Agents run `.ai/bin/ai-task`, the workspace's own
copy, from the workspace root: a sandbox can reach only that folder, and a literal path is easy to
grant. Both behave the same: the workspace is the nearest folder above you that has `.ai/`.

`close` refuses while any worktree has uncommitted or unpushed work, or the worklog is empty, so
nothing is lost by accident. `--force` overrides that; a branch with unpushed commits is still kept.

## Work without repositories

Create a folder, start a session in it and describe the problem:

```bash
mkdir ~/wifi_troubleshooting && cd ~/wifi_troubleshooting
claude          # or agy, gemini
```

> My laptop's wifi drops every evening around 7pm. Set up my workflow for this problem and record
> what we know so far.

With no `.ai/` in the folder or above it, the agent runs `init` there, writes the problem file and
starts a task with no repositories. It does not create a workspace in your home directory or `/`
(every folder below would belong to it) or inside a git checkout; it asks where to put it instead.

## Sandboxes

Start sandboxed sessions (Claude Code's sandbox, `agy --sandbox`, Gemini CLI's Docker sandbox) at
the workspace root. A sandbox can reach only the folder it starts in, and a worktree's git data
lives in the main clone, so a session started inside `.tasks/<id>/` cannot commit.

Gemini CLI's Docker sandbox mounts only the launch folder and replaces `HOME`, so it does not see
`~/.gemini/GEMINI.md` or installed skills. `.ai/bin/ai-task` still works there because it lives in
the workspace, and `init`, `clone` and `start` copy your git name and email into each clone so that
commits work.

## The `ai-task` command

`install.sh` manages one file outside the tools' own folders: the link `~/.local/bin/ai-task`. The
rules it follows:

- **When.** A user-scope install (`--scope user`) of any tool that includes the `ai-task` skill
  creates the link, and `--update` adds it to an install made before the link existed. A project
  install never writes into your home directory. A `--copy` install gets no link, since the link
  would point into the clone that `--copy` is meant to avoid.
- **What it points at.** The script in your clone (`skills/ai-task/scripts/ai-task`), so a
  `git pull` updates the command too. `install.sh --status` shows where it points.
- **Your files are safe.** If `~/.local/bin/ai-task` already exists and is not a link into this
  clone, the installer leaves it alone and says so; `--force` replaces it. Nothing else in
  `~/.local/bin` is touched, and `~/.zshrc`, `~/.bashrc` and `~/.profile` are never edited.
- **Removal.** `--uninstall` removes the link once no installed tool still uses the skill. For
  example, uninstalling Claude Code while Antigravity still has it keeps the link. The
  `~/.local/bin` folder itself is left in place.

## Linux and macOS

The script runs on both. On a Mac it uses the system's own bash 3.2 and BSD tools, so Homebrew
`bash` or GNU `sed` are not needed. CI runs the tests on Linux and again under bash 3.2 without GNU
tools. To check a machine yourself, run the tests from your clone (no network, nothing outside a
temporary folder is touched):

```bash
~/ai_agents/skills/ai-task/tests/run_tests.sh
```

Changing the script: keep it to bash 3.2 and POSIX tools. That means no `mapfile`, no `sed -i`, no
`readlink -f` and no `\+` in `sed`, and an array that can be empty is expanded as
`${a[@]+"${a[@]}"}`. The tests reject the common slips.

## Updating

```bash
cd ~/ai_agents && git pull && ./install.sh --update
cd ~/dev && ai-task init                    # refresh the workspace's copy of the script
```
