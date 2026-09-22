#!/usr/bin/env python3
"""check_repo.py — static checks on the agents and skills library (stdlib only).

  python3 tools/ci/check_repo.py          # exit 0 = clean, 1 = problems found

Checks:
  skills/<n>/SKILL.md   frontmatter parses; name == folder; name follows the Agent Skills rules
                        (lowercase letters, digits, single hyphens, <= 64 chars); description
                        present and <= 1024 chars
  agents/<n>/AGENT.md   frontmatter parses; name == folder; description present; every entry in
                        `skills:` exists under skills/; every entry in `tools:` is a neutral tool
                        name known to tools/render.py
  AGENTS.md             the install.sh managed block matches what render.py generates now
                        (catches a new or renamed agent/skill without a re-run of install.sh)

Reuses tools/render.py's parser so CI validates exactly what install.sh renders.
"""
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import render  # noqa: E402  (path set above)

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
NEUTRAL_TOOLS = set(render.TOOL_MAP["claude"])  # every harness maps the same neutral names

problems = []


def parse(path):
    """render.parse() calls sys.exit on bad frontmatter; turn that into a recorded problem."""
    try:
        return render.parse(path)
    except SystemExit as e:
        problems.append(str(e))
        return None, None


def check_name(path, folder, name):
    if name != folder:
        problems.append(f"{path}: name '{name}' does not match folder '{folder}'")
    if len(name) > 64 or not NAME_RE.match(name):
        problems.append(f"{path}: name '{name}' must be lowercase letters/digits/single hyphens, <= 64 chars")


def check_skills():
    skills_dir = os.path.join(ROOT, "skills")
    for folder in sorted(os.listdir(skills_dir)):
        path = os.path.join("skills", folder, "SKILL.md")
        if not os.path.isfile(os.path.join(ROOT, path)):
            problems.append(f"skills/{folder}: missing SKILL.md")
            continue
        fm, _ = parse(os.path.join(ROOT, path))
        if fm is None:
            continue
        check_name(path, folder, fm["name"])
        desc = fm.get("description", "")
        if not desc.strip():
            problems.append(f"{path}: description is empty")
        elif len(desc) > 1024:
            problems.append(f"{path}: description is {len(desc)} chars (limit 1024)")


def check_agents():
    agents_dir = os.path.join(ROOT, "agents")
    skills = set(os.listdir(os.path.join(ROOT, "skills")))
    for folder in sorted(os.listdir(agents_dir)):
        path = os.path.join("agents", folder, "AGENT.md")
        if not os.path.isfile(os.path.join(ROOT, path)):
            problems.append(f"agents/{folder}: missing AGENT.md")
            continue
        fm, _ = parse(os.path.join(ROOT, path))
        if fm is None:
            continue
        check_name(path, folder, fm["name"])
        if not fm.get("description", "").strip():
            problems.append(f"{path}: description is empty")
        for s in fm.get("skills", []):
            if s not in skills:
                problems.append(f"{path}: skills: lists '{s}', which is not a folder under skills/")
        for t in fm.get("tools", []):
            if t not in NEUTRAL_TOOLS:
                problems.append(f"{path}: tools: '{t}' is not a neutral tool name ({', '.join(sorted(NEUTRAL_TOOLS))})")


def check_agents_md():
    """The committed managed block must equal render.py's output (install.sh writes the same)."""
    expected = render.render_agents_md(os.path.join(ROOT, "agents"), os.path.join(ROOT, "skills")).strip("\n")
    text = open(os.path.join(ROOT, "AGENTS.md"), encoding="utf-8").read()
    m = re.search(r"<!-- BEGIN managed by install\.sh.*?<!-- END managed by install\.sh -->", text, re.S)
    if not m:
        problems.append("AGENTS.md: managed block not found")
    elif m.group(0).strip("\n") != expected:
        problems.append("AGENTS.md: managed block is out of date; run ./install.sh --harness antigravity (any harness) and commit AGENTS.md")


def main():
    check_skills()
    check_agents()
    check_agents_md()
    if problems:
        print(f"{len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    n_skills = len(os.listdir(os.path.join(ROOT, "skills")))
    n_agents = len(os.listdir(os.path.join(ROOT, "agents")))
    print(f"ok: {n_skills} skills, {n_agents} agents, AGENTS.md block in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())
