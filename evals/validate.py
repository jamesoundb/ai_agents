#!/usr/bin/env python3
"""Structural validator for the eval suite.

`claude plugin eval` costs API credits on every run, so a typo in a grader should be
caught here rather than after a 15-run billing cycle. This checks everything that can
be checked without the CLI:

  - each case has a prompt.md and at least one grader (an empty graders/ dir always
    passes, which is worse than no case at all)
  - frontmatter is valid YAML and its fields have the right types
  - every grader `type` is one the runner implements, with its required fields
  - every regex actually compiles, and `target` is a value the runner recognizes
  - paths referenced by case.yaml (scaffold_script, add_dirs) exist on disk

It does NOT verify the schema against a running CLI -- see README.md.

Exit code 0 = clean, 1 = errors found. Warnings alone do not fail the run.
"""
import os
import re
import sys

try:
    import yaml
except ImportError:                                    # pragma: no cover - environment guard
    sys.exit("evals/validate.py needs PyYAML to parse case frontmatter: pip install pyyaml")

HERE = os.path.dirname(os.path.abspath(__file__))

GRADER_TYPES = {
    "regex": {"required": {"pattern"}, "optional": {"target", "flags", "match"}},
    "tool_used": {"required": {"tool"}, "optional": {"target", "input_match", "min", "max"}},
    "tool_order": {"required": {"before", "after"}, "optional": {"target"}},
    "file_exists": {"required": {"path"}, "optional": {"target"}},
    "llm": {"required": {"criteria"}, "optional": {"target", "focus"}},
    "baseline": {"required": {"baseline_file", "criteria"}, "optional": {"target"}},
}
TARGETS = {"last_message", "trace", "files", "mock_calls"}
MATCH_MODES = {"contains", "not_contains"}          # plus "count:N"
PROMPT_FIELDS = {
    "name": str, "agent": str, "tags": list, "plugins": list, "runs": int, "max_turns": int,
    "timeout_seconds": int, "allowed_tools": list, "model": str,
    "append_system_prompt": str, "env": dict,
}

errors, warnings = [], []


def err(where, msg):
    errors.append(f"{where}: {msg}")


def warn(where, msg):
    warnings.append(f"{where}: {msg}")


def split_frontmatter(path):
    """Return (frontmatter_dict, body). Raises ValueError when the block is malformed."""
    text = open(path, encoding="utf-8").read()
    if not text.startswith("---"):
        raise ValueError("file does not start with a '---' frontmatter block")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("frontmatter block is not closed with '---'")
    data = yaml.safe_load(parts[1])
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"frontmatter is {type(data).__name__}, expected a mapping")
    return data, parts[2]


def check_prompt(case, path):
    rel = os.path.relpath(path, HERE)
    try:
        fm, body = split_frontmatter(path)
    except (ValueError, yaml.YAMLError) as e:
        err(rel, str(e))
        return
    if not body.strip():
        err(rel, "prompt body is empty -- the agent would receive no instruction")
    for k, v in fm.items():
        if k not in PROMPT_FIELDS:
            warn(rel, f"unknown frontmatter field {k!r}")
        elif not isinstance(v, PROMPT_FIELDS[k]):
            err(rel, f"field {k!r} is {type(v).__name__}, expected {PROMPT_FIELDS[k].__name__}")
    if "name" not in fm:
        warn(rel, "no 'name' -- the report will fall back to the directory name")
    for key in ("runs", "max_turns", "timeout_seconds"):
        if key in fm and fm[key] < 1:
            err(rel, f"{key} must be >= 1, got {fm[key]}")
    if fm.get("runs", 3) < 3:
        warn(rel, f"runs={fm.get('runs')}: fewer than 3 gives a weak signal on a stochastic agent")
    for name in fm.get("env", {}):
        if not name.startswith("EVAL_"):
            err(rel, f"env var {name!r} must be EVAL_-prefixed to reach the case")


def check_grader(case, path):
    rel = os.path.relpath(path, HERE)
    try:
        fm, _ = split_frontmatter(path)
    except (ValueError, yaml.YAMLError) as e:
        err(rel, str(e))
        return
    gtype = fm.get("type")
    if gtype is None:
        err(rel, "grader has no 'type'")
        return
    if gtype not in GRADER_TYPES:
        err(rel, f"unknown grader type {gtype!r}; known: {', '.join(sorted(GRADER_TYPES))}")
        return
    spec = GRADER_TYPES[gtype]
    for field in spec["required"]:
        if field not in fm:
            err(rel, f"type {gtype!r} requires field {field!r}")
    for field in fm:
        if field != "type" and field not in spec["required"] | spec["optional"]:
            warn(rel, f"field {field!r} is not used by type {gtype!r}")

    target = fm.get("target")
    if isinstance(target, str) and target not in TARGETS:
        err(rel, f"target {target!r} is not one of {', '.join(sorted(TARGETS))} (or a {{source: file, path: ...}} mapping)")
    elif isinstance(target, dict) and target.get("source") != "file":
        err(rel, f"mapping target must be {{source: file, path: ...}}, got {target!r}")

    for field in ("pattern", "input_match"):
        if field in fm:
            try:
                re.compile(fm[field])
            except re.error as e:
                err(rel, f"{field} is not a valid regex: {e}")

    match = fm.get("match")
    if match is not None and match not in MATCH_MODES and not str(match).startswith("count:"):
        err(rel, f"match {match!r} must be one of {', '.join(sorted(MATCH_MODES))} or 'count:N'")

    if gtype == "tool_used":
        lo, hi = fm.get("min", 1), fm.get("max")
        if hi is not None and lo > hi:
            err(rel, f"min ({lo}) is greater than max ({hi})")
    if gtype == "llm" and len(str(fm.get("criteria", "")).strip()) < 40:
        warn(rel, "llm criteria is very short; vague criteria are the main source of judge flakiness")


def check_case_yaml(case, path):
    rel = os.path.relpath(path, HERE)
    try:
        data = yaml.safe_load(open(path, encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        err(rel, f"invalid YAML: {e}")
        return
    ctx = data.get("context", {})
    if not isinstance(ctx, dict):
        err(rel, "'context' must be a mapping")
        return
    script = ctx.get("scaffold_script")
    if script:
        resolved = os.path.normpath(os.path.join(os.path.dirname(path), script))
        if not os.path.exists(resolved):
            err(rel, f"scaffold_script {script!r} does not exist ({resolved})")
        elif not os.access(resolved, os.X_OK):
            err(rel, f"scaffold_script {script!r} is not executable")
    for d in ctx.get("add_dirs", []) or []:
        resolved = os.path.normpath(os.path.join(os.path.dirname(path), d))
        if not os.path.isdir(resolved):
            err(rel, f"add_dirs entry {d!r} is not a directory ({resolved})")
        elif not any(os.scandir(resolved)):
            err(rel, f"add_dirs entry {d!r} is empty")


def main():
    cases = sorted(
        d for d in os.listdir(HERE)
        if os.path.isdir(os.path.join(HERE, d)) and d not in {"fixtures", "scaffold", "results", "__pycache__"}
    )
    if not cases:
        print("no eval cases found", file=sys.stderr)
        return 1

    for case in cases:
        cdir = os.path.join(HERE, case)
        prompt = os.path.join(cdir, "prompt.md")
        if not os.path.exists(prompt):
            err(case, "no prompt.md -- the runner will not discover this case")
        else:
            check_prompt(case, prompt)

        gdir = os.path.join(cdir, "graders")
        if not os.path.isdir(gdir):
            err(case, "no graders/ directory")
        else:
            graders = sorted(f for f in os.listdir(gdir) if f.endswith(".md"))
            if not graders:
                err(case, "graders/ is empty -- a case with no graders always passes")
            for g in graders:
                check_grader(case, os.path.join(gdir, g))

        cy = os.path.join(cdir, "case.yaml")
        if os.path.exists(cy):
            check_case_yaml(case, cy)

        n = len(os.listdir(gdir)) if os.path.isdir(gdir) else 0
        print(f"  {case}: {n} grader(s)")

    for w in warnings:
        print(f"  warn  {w}")
    for e in errors:
        print(f"  ERROR {e}")
    print(f"\n{len(cases)} case(s), {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
