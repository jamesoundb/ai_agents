#!/usr/bin/env python3
"""Run the eval cases on a coding harness, from the same case files for every harness.

  python3 evals/run_evals.py --harness claude --case 'agent-helm-*' --runs 1
  python3 evals/run_evals.py --harness antigravity --case code-graph-callers   # = run_agy.py

claude: drives `claude plugin eval`. This repository is not a Claude plugin as it stands (agents
are agents/<name>/AGENT.md, rendered by install.sh), so each invocation first builds one:
`install.sh --harness claude --plugin --copy` into a temporary directory, with evals/ copied in
beside the agents and skills. Skills' tests/ folders are left out of that build: they hold the
sources of some fixtures (cluster-graph/tests/faults.yaml lists every planted fault), and an
agent that reads them is grading itself.

`claude plugin eval` has no per-case agent. A case names its agent in case.yaml
`context.agent`; the runner groups the cases by agent and gives the plugin a settings.json
`{"agent": "ai-agents:<name>"}` for that group, which runs the main thread as that agent (the
plugin `agent` setting, observed on CLI 2.1.331; not documented for evals). Cases without an agent
run with no settings.json, on the skills alone.

The scaffold gets no environment from `claude plugin eval`, so the runner writes this checkout's
path to evals/scaffold/.repo-root in the build, where scaffold/repo-root.sh finds it.

antigravity: hands every other argument to run_agy.py, which drives `agy` directly.

Exit code: 0 every case scored 1.0, 1 a case scored lower or failed to run, 2 setup problem.
"""
import argparse
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from run_agy import NOT_CASES, case_harnesses, load_frontmatter  # noqa: E402

PLUGIN = "ai-agents"                       # the plugin name install.sh --plugin writes
GATED_TOOLS = {"Bash", "Write", "Edit", "WebFetch"}   # need an operator grant (--allow-tools)


def claude_bin(explicit):
    """The claude CLI: --claude-bin, $CLAUDE_BIN, or `claude` on PATH."""
    for c in (explicit, os.environ.get("CLAUDE_BIN"), shutil.which("claude")):
        if c and os.access(c, os.X_OK):
            return c
    return None


def case_context(case_dir):
    cy = os.path.join(case_dir, "case.yaml")
    if not os.path.exists(cy):
        return {}
    return (yaml.safe_load(open(cy, encoding="utf-8")) or {}).get("context", {}) or {}


def build_plugin(dest):
    """Install this checkout as a Claude plugin under dest; return the plugin directory."""
    r = subprocess.run([os.path.join(REPO, "install.sh"), "--harness", "claude", "--target", dest,
                        "--plugin", "--copy", "--no-setup"], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"install.sh failed:\n{r.stdout}\n{r.stderr}")
    plugin = os.path.join(dest, ".claude", "skills", PLUGIN)
    for skill in os.listdir(os.path.join(plugin, "skills")):
        shutil.rmtree(os.path.join(plugin, "skills", skill, "tests"), ignore_errors=True)
    generated = shutil.ignore_patterns("results", "__pycache__", ".ast-graph", ".repo-root")

    def ignore(d, names):
        # evals/tests (validate.py's unit tests) only; a fixture's own tests/ stays.
        return set(generated(d, names)) | ({"tests"} if os.path.samefile(d, HERE) else set())

    shutil.copytree(HERE, os.path.join(plugin, "evals"), ignore=ignore)
    with open(os.path.join(plugin, "evals", "scaffold", ".repo-root"), "w", encoding="utf-8") as f:
        f.write(REPO)
    return plugin


def set_agent(plugin, agent):
    """Point the plugin's settings.json at agent (None: no main-thread agent)."""
    path = os.path.join(plugin, "settings.json")
    if agent:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"agent": f"{PLUGIN}:{agent}"}, f)
    elif os.path.exists(path):
        os.remove(path)


def run_claude(args, rest):
    cli = claude_bin(args.claude_bin)
    if not cli:
        print("claude CLI not found: put it on PATH, set CLAUDE_BIN or pass --claude-bin", file=sys.stderr)
        return 2
    cases = sorted(d for d in os.listdir(HERE)
                   if os.path.isdir(os.path.join(HERE, d)) and d not in NOT_CASES)
    if args.case:
        cases = [c for c in cases if any(fnmatch.fnmatch(c, p) for p in args.case)]
    skipped = [c for c in cases if "claude" not in case_harnesses(os.path.join(HERE, c))]
    cases = [c for c in cases if c not in skipped]
    for c in skipped:
        print(f"skip {c}: case.yaml context.harnesses leaves out claude")
    if not cases:
        print("no matching cases", file=sys.stderr)
        return 1

    out_dir = args.output_dir or tempfile.mkdtemp(prefix="claude-evals-")
    os.makedirs(out_dir, exist_ok=True)
    build = tempfile.mkdtemp(prefix="claude-evals-plugin-")
    plugin = build_plugin(build)
    summary, worst = [], 0
    try:
        # One plugin build, one settings.json per agent: group the cases by their agent.
        by_agent = {}
        for c in cases:
            by_agent.setdefault(case_context(os.path.join(HERE, c)).get("agent"), []).append(c)
        for agent, group in sorted(by_agent.items(), key=lambda kv: kv[0] or ""):
            set_agent(plugin, agent)
            for c in group:
                fm, _ = load_frontmatter(os.path.join(HERE, c, "prompt.md"))
                # `--case` matches the case name: (default: the directory name). Brackets make a
                # glob of it that matches only that literal name.
                name = fm.get("name") or c
                glob = "".join(f"[{ch}]" if ch in "*?[]" else ch for ch in name)
                res = os.path.join(out_dir, f"{c}.json")
                cmd = [cli, "plugin", "eval", plugin, "--case", glob, "--ablation", "none",
                       "--scaffold", "--trust-plugin", "--no-publish", "--json", res,
                       "--output-dir", os.path.join(out_dir, c)]
                tools = sorted(GATED_TOOLS & set(fm.get("allowed_tools") or []))
                if tools:
                    cmd += ["--allow-tools", *tools]
                if args.runs:
                    cmd += ["--runs", str(args.runs)]
                if args.model:
                    cmd += ["--model", args.model]
                if args.keep_temp:
                    cmd += ["--keep-temp"]
                cmd += rest
                print(f"== {c}  (agent: {agent or 'none'})", flush=True)
                r = subprocess.run(cmd, capture_output=True, text=True)
                summary.append(report_case(c, res, r))
                worst = max(worst, summary[-1]["status"])
    finally:
        shutil.rmtree(build, ignore_errors=True)
    print(f"\nresults in {out_dir}")
    return worst


def report_case(case, res, proc):
    """Print one case's runs; status 0 = every run scored 1.0, 1 = anything less."""
    if not os.path.exists(res):
        print(f"  no result ({proc.returncode}): {(proc.stdout + proc.stderr).strip()[-600:]}")
        return {"case": case, "status": 1}
    data = json.load(open(res, encoding="utf-8"))
    if not data.get("cases"):
        print(f"  case did not load: {(proc.stdout + proc.stderr).strip()[-600:]}")
        return {"case": case, "status": 1}
    status = 0
    for i, run in enumerate(data["cases"][0]["arms"]["with"], 1):
        failed = [g["name"] for g in run.get("graders", []) if not g.get("passed")]
        if run.get("error") or run.get("score", 0) < 1:
            status = 1
        print(f"  run {i}: score {run.get('score', 0):.2f}  turns {run.get('turns')}  "
              f"${run.get('costUsd', 0):.3f}  {run.get('durationSeconds')}s"
              + (f"  ERROR {run['error']}" if run.get("error") else "")
              + (f"  FAIL {', '.join(failed)}" if failed else ""))
    return {"case": case, "status": status}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--harness", required=True, choices=["claude", "antigravity"])
    ap.add_argument("--case", action="append", help="glob of case directory names (repeatable)")
    ap.add_argument("--runs", type=int, help="override the per-case runs:")
    ap.add_argument("--model", help="claude: model for every case")
    ap.add_argument("--keep-temp", action="store_true", help="keep the run workspaces for inspection")
    ap.add_argument("--output-dir", help="claude: where the per-case result JSON goes")
    ap.add_argument("--claude-bin", help="claude: path to the claude CLI")
    args, rest = ap.parse_known_args()
    if args.harness == "antigravity":
        # run_agy.py owns this harness; pass the shared options through unchanged.
        fwd = [a for c in (args.case or []) for a in ("--case", c)]
        fwd += (["--runs", str(args.runs)] if args.runs else []) + (["--keep-temp"] if args.keep_temp else [])
        return subprocess.call([sys.executable, os.path.join(HERE, "run_agy.py"), *fwd, *rest])
    return run_claude(args, rest)


if __name__ == "__main__":
    sys.exit(main())
