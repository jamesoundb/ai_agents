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
    lines = ["---", f"name: {fm['name']}", f"description: {folded(fm['description'])}"]
    if tools:
        lines.append("tools: " + ", ".join(dict.fromkeys(tools)))
    if fm.get("model") and fm["model"] != "inherit":
        lines.append(f"model: {fm['model']}")
    if fm.get("skills"):
        lines.append("skills: [" + ", ".join(fm["skills"]) + "]")
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


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    cmd = argv[0]
    if cmd == "agents-md":
        sys.stdout.write(render_agents_md(argv[1], argv[2]))
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
