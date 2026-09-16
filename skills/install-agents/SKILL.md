---
name: install-agents
description: >
  Install or update this repository's AI agents and skills into the developer's coding harness
  (Claude Code, OpenAI Codex, Gemini CLI, Google Antigravity, GitHub Copilot), for this checkout
  or for another project on disk. Use when a developer asks to install, set up, enable, update or
  remove the agents/skills, asks how to use them in their tool, or on a first session in this
  repository. Wraps install.sh so the result is deterministic.
allowed-tools: Bash(*/install-agents/scripts/install.sh *), Bash(*/install.sh *), Bash(ls *), Bash(cat *), Read, Glob
---

# install-agents: let the harness install the agents for the developer

You are running inside a coding harness. Your job is to install the agents and skills from this
repository into that harness (or another one the developer names), verify the result, and tell
the developer how to invoke them. The installer is a script; you gather the parameters and run it.
Do not hand-copy files or write harness config yourself.

## 1. Gather parameters (ask only for what you cannot infer)

- **Harness**: you know which product you are (Claude Code, Codex, Gemini CLI, Antigravity,
  Copilot). Default to that one. If the developer wants several, use a comma list or `all`.
- **Scope**: `project` (files in the target repo, shareable with the team) or `user` (files in
  the developer's home directory, available in every project). Default `project`.
- **Target**: the repository to install into. Default: the current working directory. If the
  developer names another project, pass its absolute path with `--target`.
- **Link or copy**: symlinks (default) keep installs in sync with this clone; use `--copy` on
  Windows without developer mode, in CI images, or when the clone will be deleted.
- **Which agents/skills**: everything by default. `--agents a,b` / `--skills x,y` to narrow.

## 2. Run the installer

```bash
scripts/install.sh --harness <claude|codex|gemini|antigravity|copilot|all> [--scope user] [--target /abs/path] [--copy] [--agents ...] [--skills ...]
```

`scripts/install.sh` (relative to this skill folder) resolves this skill's real location and runs
the repository's `install.sh`, so it works whether the skill is symlinked or opened in place.
Add `--uninstall` to remove what a previous run installed. Re-running is idempotent.

## 3. Verify (mandatory)

- List what was created and confirm each path exists, e.g. `ls -la <target>/.claude/skills`,
  `cat <target>/.claude/agents/<agent>.md | head -20` (adjust per harness; the installer prints
  every path it wrote).
- Confirm `AGENTS.md` in the target contains the managed block (project scope).
- If the target is a git repository, confirm `.ast-graph/` is in its `.gitignore`; add the line if
  the developer agrees (the code-graph skill writes its graph there).

## 4. Tell the developer how to invoke

| harness | agents | skills |
|---|---|---|
| Claude Code | delegated automatically by description, or name the subagent in a prompt | `/skill-name` or automatic |
| Codex | `$agent-name` (installed as a persona skill) | `$skill-name` |
| Gemini CLI | `/agent-name` persona skill or automatic | automatic by description |
| Antigravity | `/agents` picker, `agy --agent <name>`, or `invoke_subagent` | automatic by description |
| GitHub Copilot | custom agent picker (`.github/agents/*.agent.md`) | automatic by description |

Finish with the exact install command you ran so the developer can repeat it without an LLM.

## Notes

- If `python3` is missing the installer stops; tell the developer to install Python 3.10+ (the
  installer itself is standard-library only, but the skills' tree-sitter engine needs 3.10).
- Installing both Codex and Antigravity into one project creates a persona skill and a native
  agent with the same name; harmless, but say so.
- Harness folder layouts are listed in the repository README; if a harness has moved its folders
  since 2026-09-07, update `install.sh` rather than working around it.
