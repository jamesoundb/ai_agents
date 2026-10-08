#!/usr/bin/env python3
"""
render.py — render a harness-neutral agents/<name>/AGENT.md into vendor formats.

  render.py claude   AGENT.md      -> Claude Code subagent (.claude/agents/<name>.md)
  render.py copilot  AGENT.md      -> GitHub Copilot custom agent (.github/agents/<name>.agent.md)
  render.py antigravity AGENT.md [SKILLS_PREFIX]
                                   -> Antigravity CLI custom agent (.agents/agents/<name>/agent.md);
                                      SKILLS_PREFIX is where the skills were installed
                                      (default .agents/skills, workspace-relative)
  render.py skill    AGENT.md      -> "agent-as-skill" SKILL.md for harnesses without agent files
                                      (Codex, Gemini CLI): invoke as $<name> / /<name>
  render.py agents-md AGENTS_DIR SKILLS_DIR -> the managed block for a root AGENTS.md
  render.py plugin-manifest HARNESS VERSION -> the plugin/extension manifest for install.sh --plugin
                                      (claude, antigravity, gemini, copilot, codex)
  render.py codex-marketplace add|remove FILE [PATH]
                                   -> add or remove our entry in a Codex marketplace.json; PATH is
                                      the plugin folder relative to the marketplace root
  render.py hook HARNESS plugin    -> the plugin's hooks file (claude, gemini, antigravity): session-start
                                      code-graph build
  render.py hook HARNESS add|remove FILE [COMMAND]
                                   -> add or remove our SessionStart entry in a settings file (Claude or
                                      Gemini settings.json, Antigravity hooks.json), keeping everything else

Only the standard library is used. AGENT.md frontmatter supports scalars, `[a, b]` lists and
`>` folded multi-line strings; that is deliberately all the canonical file needs.
"""
import os
import re
import sys

TOOL_MAP = {
    "claude": {"shell": "Bash", "read": "Read", "glob": "Glob", "grep": "Grep", "edit": "Edit",
               "write": "Write", "web": "WebFetch", "search": "WebSearch"},
    # Copilot custom-agent tool names (see docs.github.com custom-agents-configuration)
    "copilot": {"shell": "shell", "read": "read", "glob": "search", "grep": "search", "edit": "edit",
                "write": "edit", "web": "web", "search": "web"},
    # Antigravity tool names (antigravity.google/docs/subagents). The docs warn that an unmapped
    # tool name can make a subagent hang, so keep this list to documented tools.
    "antigravity": {"shell": "run_command", "read": "view_file", "glob": "find_by_name",
                    "grep": "grep_search", "edit": "replace_file_content", "write": "write_to_file",
                    "web": "read_url_content", "search": "search_web"},
}


def parse(path):
    text = open(path, encoding="utf-8").read()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, flags=re.S)
    if not m:
        sys.exit(f"{path}: missing frontmatter")
    fm, body = {}, m.group(2).strip("\n")
    lines = m.group(1).splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        km = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if not km:
            i += 1
            continue
        key, val = km.group(1), km.group(2).strip()
        if val in (">", "|"):
            buf = []
            i += 1
            while i < len(lines) and (lines[i].startswith(" ") or lines[i] == ""):
                buf.append(lines[i].strip())
                i += 1
            fm[key] = (" " if val == ">" else "\n").join(b for b in buf if b)
            continue
        if val.startswith("[") and val.endswith("]"):
            fm[key] = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
        elif val.lower() in ("true", "false"):
            fm[key] = val.lower() == "true"
        else:
            fm[key] = val.strip("'\"")
        i += 1
    for req in ("name", "description"):
        if req not in fm:
            sys.exit(f"{path}: frontmatter needs '{req}'")
    return fm, body


def folded(text, indent="  "):
    """Emit a long string as a YAML folded scalar, wrapped at ~96 columns."""
    words, out, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > 96:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        out.append(cur)
    return ">\n" + "\n".join(indent + l for l in out)


def render_claude(fm, body):
    tools = [TOOL_MAP["claude"].get(t, t) for t in fm.get("tools", [])]
    # Claude Code injects the full SKILL.md of every skill named in `skills:` into the subagent's context
    # on every request. An agent's optional `preload:` names the few it always needs; the rest stay
    # installed and are loaded on demand through the Skill tool.
    skills = fm.get("skills") or []
    preload = fm.get("preload", skills)
    if any(s not in preload for s in skills):
        tools.append("Skill")
    lines = ["---", f"name: {fm['name']}", f"description: {folded(fm['description'])}"]
    if tools:
        lines.append("tools: " + ", ".join(dict.fromkeys(tools)))
    if fm.get("model") and fm["model"] != "inherit":
        lines.append(f"model: {fm['model']}")
    if preload:
        lines.append("skills: [" + ", ".join(preload) + "]")
    lines.append("permissionMode: default")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body + "\n"


def render_copilot(fm, body):
    tools = list(dict.fromkeys(TOOL_MAP["copilot"].get(t, t) for t in fm.get("tools", [])))
    lines = ["---", f"name: {fm['name']}", f"description: {folded(fm['description'])}"]
    if tools:
        lines.append("tools: [" + ", ".join(f"'{t}'" for t in tools) + "]")
    if fm.get("model") and fm["model"] != "inherit":
        lines.append(f"model: {fm['model']}")
    lines.append("---")
    note = ""
    if fm.get("skills"):
        note = ("\n\n## Skills\n\nUse these installed skills (in `.github/skills/`): "
                + ", ".join(f"`{s}`" for s in fm["skills"]) + ".\n")
    return "\n".join(lines) + "\n\n" + body + note


def render_antigravity(fm, body, skills_prefix=".agents/skills"):
    tools = list(dict.fromkeys(TOOL_MAP["antigravity"].get(t, t) for t in fm.get("tools", [])))
    lines = ["---", f"name: {fm['name']}", f"description: {folded(fm['description'])}"]
    if tools:
        lines.append("tools:")
        lines += [f"  - {t}" for t in tools]
    lines.append("mainAgent: true")
    lines.append("subagent: true")
    lines.append(f"model: {fm.get('model') or 'inherit'}")
    # read-only agents keep shell commands sandboxed; others may auto-run safe commands
    lines.append("commandExecutionPolicy: " + ("sandbox" if fm.get("readonly") else "auto"))
    if fm.get("skills"):
        lines.append("skills:")
        lines += [f"  - {skills_prefix.rstrip('/')}/{s}" for s in fm["skills"]]
    lines.append("---")
    return "\n".join(lines) + "\n\n# System Prompt\n\n" + body + "\n"


def render_skill(fm, body):
    """Wrap the agent persona as a standard Agent Skill so any skills-capable harness can run it."""
    lines = ["---", f"name: {fm['name']}",
             f"description: {folded('Agent persona: ' + fm['description'] + ' Invoke to adopt this role for the current task.')}",
             "---", "",
             f"# {fm['name']} (agent persona)", "",
             "Adopt the role below for the rest of this task. Skills this persona relies on: "
             + ", ".join(f"`{s}`" for s in fm.get("skills", [])) + ".",
             ("Constraints: read-only; report findings, do not edit files." if fm.get("readonly") else ""),
             "", body, ""]
    return "\n".join(lines)


def summary(desc, limit=220):
    """First sentences of a description, up to ~limit characters."""
    out, n = "", 0
    for sent in re.split(r"(?<=\.)\s+", desc):
        if n >= 2 and len(out) + len(sent) > limit:
            break
        out = f"{out} {sent}".strip()
        n += 1
    return out


def render_agents_md(agents_dir, skills_dir):
    out = ["<!-- BEGIN managed by install.sh (agents repo); edits inside this block are overwritten -->",
           "## AI agents and skills installed in this repository", "",
           "Skills follow the Agent Skills standard (a folder with SKILL.md). Agents are personas; on",
           "harnesses without agent files they are installed as `<name>` skills and invoked by name.", "",
           "| agent | use it for |", "|---|---|"]
    for name in sorted(os.listdir(agents_dir)):
        p = os.path.join(agents_dir, name, "AGENT.md")
        if os.path.exists(p):
            fm, _ = parse(p)
            out.append(f"| `{fm['name']}` | {summary(fm['description'])} |")
    out += ["", "| skill | use it for |", "|---|---|"]
    for name in sorted(os.listdir(skills_dir)):
        p = os.path.join(skills_dir, name, "SKILL.md")
        if os.path.exists(p):
            fm, _ = parse(p)
            out.append(f"| `{fm['name']}` | {summary(fm['description'])} |")
    out += ["", "Rules for every agent working here:", "",
            "- Prefer the `code-skeleton` skill over printing whole files; read only the line ranges you need.",
            "- For dependency or impact questions use the `code-graph` / `blast-radius` skills and cite",
            "  `file:line`, edge type and confidence from their output.",
            "- The graph artifact `.ast-graph/` is generated; never commit it.",
            "<!-- END managed by install.sh -->"]
    return "\n".join(out) + "\n"


# Plugin bundles (install.sh --plugin). One folder per harness holds skills/ and the rendered agents
# next to the manifest that harness reads. Each format was checked against the harness itself on
# 2026-10-01: Antigravity (`agy plugin validate`, `agy agents`), Claude Code (`claude plugin
# validate`, `claude plugin details`), Gemini CLI (`gemini extensions validate`, `gemini skills
# list`); Copilot from VS Code's plugin loader, Codex from its published plugin spec.
PLUGIN_NAME = "ai-agents"
PLUGIN_DESCRIPTION = ("Organization agents and skills: tree-sitter code graph and blast radius, "
                      "Terraform, Kubernetes, Helm and TeamCity reviews for Google Cloud.")


def render_plugin_manifest(harness, version):
    """Manifest text for one harness. Keep fields minimal: Antigravity silently drops unknown
    top-level fields, and Copilot only treats plugin.json as its own namespaced format when a
    $schema is present -- without one VS Code reads skills/ and agents/ from the plugin root."""
    import json
    base = {"name": PLUGIN_NAME, "version": version, "description": PLUGIN_DESCRIPTION}
    if harness in ("claude", "antigravity", "gemini", "copilot"):
        return json.dumps(base, indent=2) + "\n"
    if harness == "codex":
        # Codex reads skills from the path the manifest names; personas are installed as skills.
        return json.dumps({**base, "skills": "./skills/",
                           "interface": {"displayName": "AI agents",
                                         "shortDescription": PLUGIN_DESCRIPTION,
                                         "category": "Developer Tools"}}, indent=2) + "\n"
    sys.exit(f"plugin-manifest: unknown harness {harness}")


def codex_marketplace(action, path, plugin_path=None):
    """Add (or replace) / remove our entry in a Codex marketplace.json, keeping every other entry.
    A marketplace file that would be left with no plugins and that we created is deleted."""
    import json
    data = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    plugins = [p for p in data.get("plugins", []) if p.get("name") != PLUGIN_NAME]
    if action == "add":
        data.setdefault("name", "local")
        data.setdefault("interface", {"displayName": "Local plugins"})
        plugins.append({"name": PLUGIN_NAME,
                        "source": {"source": "local", "path": plugin_path},
                        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                        "category": "Developer Tools"})
    elif action != "remove":
        sys.exit(f"codex-marketplace: unknown action {action}")
    data["plugins"] = plugins
    if not plugins and data.get("name") == "local":
        if os.path.exists(path):
            os.remove(path)
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


AUTOBUILD = "/code-graph/scripts/autobuild.sh"   # identifies our hook entry in a settings file
HOOK_NAME = "ai-agents-code-graph"                # the entry's name where the format has names


def hook_entry(harness, command):
    """Our SessionStart handler in the harness's format."""
    if harness == "gemini":   # Gemini CLI: named handler, timeout in milliseconds
        return {"name": HOOK_NAME, "type": "command", "command": command, "timeout": 10000}
    if harness == "antigravity":   # timeout in seconds
        return {"type": "command", "command": command, "timeout": 10}
    return {"type": "command", "command": command}


def plugin_hooks(harness):
    """hooks.json for the harness's plugin layout (paths relative to the plugin folder)."""
    if harness == "claude":
        cmd = '"${CLAUDE_PLUGIN_ROOT}/skills/code-graph/scripts/autobuild.sh"'
    elif harness == "gemini":
        cmd = '"${extensionPath}/skills/code-graph/scripts/autobuild.sh"'
    elif harness == "antigravity":   # runs in the folder that holds hooks.json: the plugin folder
        return {HOOK_NAME: {"SessionStart": [hook_entry(harness, "./skills/code-graph/scripts/autobuild.sh --antigravity")]}}
    else:
        sys.exit(f"hook: no plugin hooks for {harness}")
    group = {"hooks": [hook_entry(harness, cmd)]}
    if harness == "gemini":
        group = {"matcher": "*", **group}
    return {"hooks": {"SessionStart": [group]}}


def hook(harness, action, path=None, command=None):
    """The session-start hook that builds code graphs in the background (code-graph's autobuild.sh).
    `plugin` prints the plugin's hooks file; `add`/`remove` edit a settings file in place (Claude and
    Gemini settings.json, Antigravity hooks.json), touching only our entry. A file left empty by
    `remove` is deleted."""
    import json
    if harness not in ("claude", "gemini", "antigravity"):
        sys.exit(f"hook: unsupported harness {harness}")
    if action == "plugin":
        json.dump(plugin_hooks(harness), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return
    if action not in ("add", "remove"):
        sys.exit(f"hook: unknown action {action}")
    data = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            try:
                data = json.load(f)
            except ValueError as e:
                sys.exit(f"hook: {path} is not valid JSON ({e}); not touching it")
    if harness == "antigravity":   # {"<hook name>": {"<Event>": [handlers]}, ...}
        data.pop(HOOK_NAME, None)
        if action == "add":
            data[HOOK_NAME] = {"SessionStart": [hook_entry(harness, command)]}
    else:
        hooks = data.get("hooks", {})
        groups = []
        for g in hooks.get("SessionStart", []):
            keep = [h for h in g.get("hooks", []) if AUTOBUILD not in h.get("command", "")]
            if keep:
                groups.append(dict(g, hooks=keep))
        if action == "add":
            group = {"hooks": [hook_entry(harness, command)]}
            groups.append({"matcher": "*", **group} if harness == "gemini" else group)
        if groups:
            hooks["SessionStart"] = groups
        else:
            hooks.pop("SessionStart", None)
        if hooks:
            data["hooks"] = hooks
        else:
            data.pop("hooks", None)
    if not data:
        if os.path.exists(path):
            os.remove(path)
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    cmd = argv[0]
    if cmd == "agents-md":
        sys.stdout.write(render_agents_md(argv[1], argv[2]))
        return
    if cmd == "plugin-manifest":
        sys.stdout.write(render_plugin_manifest(argv[1], argv[2] if len(argv) > 2 else "0.1.0"))
        return
    if cmd == "codex-marketplace":
        codex_marketplace(argv[1], argv[2], argv[3] if len(argv) > 3 else None)
        return
    if cmd == "hook":   # hook HARNESS plugin | hook HARNESS add|remove FILE [COMMAND]
        hook(argv[1], argv[2], argv[3] if len(argv) > 3 else None, argv[4] if len(argv) > 4 else None)
        return
    if cmd == "claude-hook":   # older spelling of `hook claude`
        hook("claude", argv[1], argv[2] if len(argv) > 2 else None, argv[3] if len(argv) > 3 else None)
        return
    fm, body = parse(argv[1])
    if cmd == "antigravity":
        sys.stdout.write(render_antigravity(fm, body, argv[2] if len(argv) > 2 else ".agents/skills"))
        return
    fn = {"claude": render_claude, "copilot": render_copilot, "skill": render_skill}.get(cmd)
    if fn is None:
        sys.exit(f"unknown renderer {cmd}")
    sys.stdout.write(fn(fm, body))


if __name__ == "__main__":
    main(sys.argv[1:])
