---
name: install-agents
description: >
  Use when a developer asks to install, update or remove this repository's agents and skills in
  their coding tool (Claude Code, Codex, Gemini CLI, Antigravity, Copilot), or on a first session
  here.
allowed-tools: Bash(*/install-agents/scripts/install.sh *), Bash(*/install.sh *), Bash(ls *), Bash(cat *), Read, Glob
---

# install-agents: let the harness install the agents for the developer

You are running inside a coding harness. Your job is to install the agents and skills from this
repository into that harness (or another one the developer names), verify the result, and tell
the developer how to invoke them. The installer is a script; you gather the parameters and run it.
Do not hand-copy files or write harness config yourself.

## 1. Gather parameters (ask only for what you cannot infer)

- **Harness**: you know which product you are (Claude Code, Codex, Gemini CLI, Antigravity,
  Copilot). **Install for that one only.** Use a comma list or `all` only when the developer
  explicitly asks for more than one harness. Google Antigravity, IDE or `agy` CLI, is
  `antigravity`; `gemini` is the separate Gemini CLI. In Antigravity the result is
  `--harness antigravity --scope user`, which writes `~/.gemini/config/{agents,skills}`.
  Do not pick `gemini` because the model is Gemini: a `gemini` install goes to Gemini CLI's
  folder, which the Antigravity IDEs do not read, and it installs no agents. If the developer
  uses IntelliJ, or the installer prints the IntelliJ note, add `--agents-as-skills`
  (IntelliJ's Antigravity agent loads skills but not custom agents).
- **Scope**: `user` (files in the developer's home directory, available in every project) or
  `project` (files in one repository, e.g. to commit them for that repo's team). **Default `user`**,
  the organization's standard install (README "Quick start"). Use `project` only when the developer
  asks for a single repository or names one to install into.
- **Target** (project scope only): the repository to install into. Default: the current working
  directory. If the developer names another project, pass its absolute path with `--target`.
- **Link or copy**: symlinks (default) keep installs in sync with this clone; use `--copy` on
  Windows without developer mode, in CI images, or when the clone will be deleted.
- **Which agents/skills**: everything by default. `--agents a,b` / `--skills x,y` to narrow.
- **Loose or plugin**: loose skills and agents by default. Add `--plugin` only when the developer
  asks for a plugin. It installs one `ai-agents` plugin bundle per harness instead (README "Install as a
  plugin"). For Antigravity, prefer loose: IntelliJ's Antigravity agent reads loose skills only,
  and loads neither plugins nor custom agents. Do not install both ways, or every skill is listed
  twice. To switch, first re-run the original command with `--uninstall`. Gemini CLI plugins are
  user scope only.

## 2. Run the installer

```bash
# default: global install for this developer
scripts/install.sh --harness <claude|codex|gemini|antigravity|copilot|all> --scope user [--copy] [--agents ...] [--skills ...]
# only when a single repository was asked for
scripts/install.sh --harness <...> --scope project --target /abs/path [--copy] [--agents ...] [--skills ...]
```

Always pass `--scope` explicitly: `install.sh` on its own defaults to project scope.

`scripts/install.sh` (relative to this skill folder) resolves this skill's real location and runs
the repository's `install.sh`, so it works whether the skill is symlinked or opened in place.
Add `--uninstall` to remove what a previous run installed. Re-running is idempotent.

To **update** after `git pull`, run `scripts/install.sh --update` with no other options. It
finds every install this clone made in the home directory and re-runs each with its own
options. Add `--target DIR` for a project install. If it prints `keep ... no installer marker`,
tell the developer; add `--force` only if they confirm those files came from an older install.

## 3. Verify (mandatory)

- List what was created and confirm each path exists (the installer prints every path it wrote):
  user scope, e.g. `ls -la ~/.gemini/config/agents ~/.gemini/config/skills` (Antigravity; then
  `agy agents` must list the agents) or `ls -la ~/.claude/skills ~/.claude/agents`; project scope, e.g.
  `ls -la <target>/.claude/skills` and `cat <target>/.claude/agents/<agent>.md | head -20`
  (adjust per harness).
- With `--plugin`, check each plugin with its harness's own tool:
  `agy plugin validate ~/.gemini/config/plugins/ai-agents` (then `agy agents` after a restart),
  `claude plugin list` (shows `ai-agents@skills-dir`), `gemini extensions list`. For Copilot
  and Codex, pass on the `next` line the installer printed: the `chat.pluginLocations` setting,
  or installing from `/plugins`. Do not edit the developer's settings yourself.
- Project scope only: confirm `AGENTS.md` in the target contains the managed block, and if the
  target is a git repository confirm `.ast-graph/` is in its `.gitignore`; add the line if the
  developer agrees (the code-graph skill writes its graph there).
- User scope: remind the developer to keep the `~/ai_agents` clone where it is (skills are
  symlinks into it) and that updating is `cd ~/ai_agents && git pull` plus re-running the install.

## 4. Test that the engine actually runs here (mandatory on a new machine)

Installing only creates files. Before telling the developer it works, prove the engine runs on
*this* machine — it needs Python 3.10+ and a tree-sitter wheel for this OS and CPU, and the first
call builds a venv under `~/.cache/astgraph` (~30s, needs network).

```bash
uname -sm; bash --version | head -1; python3 -V     # record these; they go in the report
<repo>/skills/code-graph/tests/run_tests.sh          # 285 assertions, ~8s, no network
```

Then one functional check, because a green suite still runs on a copied fixture rather than on
the developer's own code. Build a graph on the checkout itself and ask it something:

```bash
<repo>/skills/code-graph/scripts/run.sh build --root <repo>
<repo>/skills/code-graph/scripts/run.sh query --root <repo> stats   # languages and file counts
<repo>/skills/code-graph/scripts/run.sh skeleton <repo>/skills/code-graph/scripts/astgraph.py
```

`query stats` must list the languages you expect for that repository, and the skeleton must print
a structure with line numbers rather than an error.

**If `uname -s` is not `Linux`, say so prominently.** As of 2026-09-29 every layer of this
repository — engine suite, evals, installer — has only ever been run on Linux, so a macOS or
Windows session is the first evidence either way. Report the three version lines above, the
suite's pass/fail count, and the `query stats` output back to the platform team **whether it
passes or fails**: a pass is the result we do not have yet, and a failure is a bug worth fixing
that day. Do not paper over a failure by falling back to grep or by skipping the step.

## 5. Tell the developer how to invoke

| harness | agents | skills |
|---|---|---|
| Claude Code | delegated automatically by description, or name the subagent in a prompt | `/skill-name` or automatic |
| Codex | `$agent-name` (installed as a persona skill) | `$skill-name` |
| Gemini CLI | `/agent-name` persona skill or automatic | automatic by description |
| Antigravity | `/agents` picker, `agy --agent <name>`, or `invoke_subagent` | automatic by description |
| GitHub Copilot | custom agent picker (`.github/agents/*.agent.md`) | automatic by description |

Finish with the exact install command you ran so the developer can repeat it without an LLM,
and the results of step 4.

## Notes

- If `python3` is missing the installer stops; tell the developer to install Python 3.10+ (the
  installer itself is standard-library only, but the skills' tree-sitter engine needs 3.10).
- Installing both Codex and Antigravity into one project creates a persona skill and a native
  agent with the same name; harmless, but say so.
- Claude Code prompts for each `run.sh` call the `ast-treesitter` subagent makes unless the
  project's `.claude/settings.json` allows `Bash(*/code-graph/scripts/run.sh *)`; mention this
  after a Claude install (do not edit the developer's settings yourself).
- Harness folder layouts are listed in the repository README; if a harness has moved its folders
  since 2026-09-07, update `install.sh` rather than working around it.
