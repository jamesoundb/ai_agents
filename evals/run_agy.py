#!/usr/bin/env python3
"""Run the eval cases in evals/ against the Antigravity CLI (`agy`).

Why this exists alongside `claude plugin eval`: our developers only have Antigravity.
An eval suite that can only be executed by a harness nobody here uses tests nothing.

The case format is shared -- same prompt.md, same graders/, same case.yaml -- so a case
written once is graded the same way on both harnesses. The only thing this file adds is
a translation layer: graders name tools in Claude Code's vocabulary (`Bash`, `Read`,
`Write`), and TOOL_ALIASES maps those onto Antigravity's (`run_command`, `view_file`,
`write_to_file`). Native Antigravity names also work if a grader prefers them.

    python3 evals/run_agy.py                       # every case
    python3 evals/run_agy.py --case skeleton-*     # a subset
    python3 evals/run_agy.py --runs 1 --no-judge   # cheap smoke pass
    python3 evals/run_agy.py --json out.json

Each run spends real model tokens. `--runs` defaults to the case's own `runs:` value.
"""
import argparse
import fnmatch
import glob as globmod
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

try:
    import yaml
except ImportError:                                    # pragma: no cover - environment guard
    sys.exit("evals/run_agy.py needs PyYAML to parse case frontmatter: pip install pyyaml")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# Claude Code tool name -> the Antigravity tools that do the same job.
TOOL_ALIASES = {
    "Bash": {"run_command", "command_status", "send_command_input"},
    "Read": {"view_file", "read_resource", "read_url_content"},
    "Write": {"write_to_file"},
    "Edit": {"replace_file_content", "multi_replace_file_content", "sed_file", "notebook_edit"},
    "Grep": {"grep_search"},
    "Glob": {"find_by_name", "list_dir"},
    "WebSearch": {"search_web"},
}
# The parameter that carries the interesting value, per Antigravity tool.
TOOL_ARG_KEYS = ("CommandLine", "Command", "AbsolutePath", "TargetFile", "Query",
                 "SearchDirectory", "Pattern", "Url")


def load_frontmatter(path):
    text = open(path, encoding="utf-8").read()
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"{path}: no frontmatter block")
    return yaml.safe_load(parts[1]) or {}, parts[2].strip()


# ---------------------------------------------------------------- running one turn
def isolated_home(base):
    """A HOME with the installed skills and credentials but WITHOUT the operator's GEMINI.md.

    Antigravity loads `~/.gemini/GEMINI.md` into every session, so an eval run measures the
    operator's personal instructions as much as the skills. That is not hypothetical: two graders
    in this suite failed on it -- one on the "confirm you have read these instructions" preamble
    and the resulting `.github/instructions.md` writes, one on "verifying your results empirically
    is also mandatory", which inflated the tool count on a two-line file.

    Everything else under ~/.gemini is symlinked, so credentials and config still work; only the
    instruction file is left out. Verified: `agy agents` lists all five agents under this HOME.

    `config/skills` and `config/agents` are the exception: they are *copied* with symlinks
    resolved rather than symlinked. Installed skills normally point back at this checkout
    (`~/.gemini/config/skills/helm-chart-review -> /home/james/ai_agents/skills/...`), and a
    tool that prints the resolved path hands the agent a route into the repository. Four runs of
    twelve in the first agent-case measurement took it: they read `evals/agent-*/graders/*.md`
    -- the assertions they were about to be scored against -- and two of those runs then passed
    every grader. Copying costs about a megabyte and removes the trail; `repo_leaks()` still
    checks, because the copy is a fence and not a proof.

    ~/.cache/astgraph is linked in as well. The scaffold warms the tree-sitter venv under the
    *real* HOME, so without this every isolated run pip-installs a 21 MB dependency tree again.
    """
    home = os.path.join(base, "home")
    gem = os.path.join(home, ".gemini")
    os.makedirs(gem, exist_ok=True)
    real = os.path.expanduser("~/.gemini")
    # Per-session memory: transcripts, conversation summaries, per-step outputs and scratch from
    # every previous agy session, including previous eval runs. Symlinking these gave each run
    # the transcripts of the ones before it -- and those transcripts name this checkout, which is
    # how runs kept finding `evals/*/graders/` after the skills and project records were cleaned.
    # A behavioural measurement has to start from nothing remembered, so each of these is a fresh
    # empty directory; everything else under antigravity*/ (bin, builtin, installation_id,
    # settings, updater, caches) is still linked so the CLI starts normally.
    session_state = {"brain", "conversations", "conversation_summaries.db", "annotations",
                     "implicit", "knowledge", "crashes", "scratch", "history.jsonl", "presence",
                     "agyhub_summaries_proto.pb", "jetbox_summaries_proto.pb", "log", "cli.log"}
    if os.path.isdir(real):
        for entry in os.listdir(real):
            if entry == "GEMINI.md":
                continue          # the whole point
            src, dst = os.path.join(real, entry), os.path.join(gem, entry)
            if os.path.exists(dst):
                continue
            if entry == "config" and os.path.isdir(src):
                os.makedirs(dst, exist_ok=True)
                for sub in os.listdir(src):
                    s, d = os.path.join(src, sub), os.path.join(dst, sub)
                    if sub in ("skills", "agents") and os.path.isdir(s):
                        shutil.copytree(s, d, symlinks=False,
                                        ignore=shutil.ignore_patterns("__pycache__", ".git", "tests"))
                    elif sub == "projects" and os.path.isdir(s):
                        # Antigravity records every project root it has seen here, this checkout
                        # among them ("file:///home/james/ai_agents"). Copying the skills cut the
                        # symlink trail but not this one: five runs of six still reached the repo,
                        # one of them opening with `grep -rn "helm-api" .../evals/` -- a fixture
                        # name that appears nowhere in its workspace. A run must not start knowing
                        # where the answer key lives, and prior-session state is a confound for a
                        # behavioural measurement anyway. Only the pathless default is kept.
                        os.makedirs(d, exist_ok=True)
                        keep_file = os.path.join(s, "default-cli-project.json")
                        if os.path.exists(keep_file):
                            shutil.copy2(keep_file, os.path.join(d, "default-cli-project.json"))
                    else:
                        os.symlink(s, d)
            elif entry.startswith("antigravity") and os.path.isdir(src):
                os.makedirs(dst, exist_ok=True)
                for sub in os.listdir(src):
                    s, d = os.path.join(src, sub), os.path.join(dst, sub)
                    if sub in session_state:
                        if os.path.isdir(s):
                            os.makedirs(d, exist_ok=True)
                    else:
                        os.symlink(s, d)
            else:
                os.symlink(src, dst)
    cache = os.path.expanduser("~/.cache/astgraph")
    if os.path.isdir(cache):
        os.makedirs(os.path.join(home, ".cache"), exist_ok=True)
        link = os.path.join(home, ".cache", "astgraph")
        if not os.path.exists(link):
            os.symlink(cache, link)
    return home


def repo_leaks(tools):
    """Tool calls that reached this checkout -- i.e. the case files and the answer key.

    A run that read its own graders cannot be scored: green proves nothing (it may have been
    written to the assertions) and red proves nothing either. Such a run is reported as an
    error, which keeps it out of the pass count instead of quietly inflating it.
    """
    return [f"{n} {a}" for n, a in tools if REPO + os.sep in a]


def run_case_once(case_dir, fm, prompt, timeout, keep, isolate=True):
    """Set up a throwaway workspace, run one agy turn in it, return the parsed trace."""
    ws = tempfile.mkdtemp(prefix="agyeval-")
    cfg = {}
    cy = os.path.join(case_dir, "case.yaml")
    if os.path.exists(cy):
        cfg = (yaml.safe_load(open(cy, encoding="utf-8")) or {}).get("context", {}) or {}

    for d in cfg.get("add_dirs", []) or []:
        src = os.path.normpath(os.path.join(case_dir, d))
        for entry in os.listdir(src):
            if entry == ".ast-graph":
                continue                      # generated; never seed a workspace with it
            s, t = os.path.join(src, entry), os.path.join(ws, entry)
            shutil.copytree(s, t) if os.path.isdir(s) else shutil.copy2(s, t)

    script = cfg.get("scaffold_script")
    if script:
        sp = os.path.normpath(os.path.join(case_dir, script))
        r = subprocess.run([sp], cwd=ws, capture_output=True, text=True,
                           env={**os.environ, "EVAL_REPO_ROOT": REPO}, timeout=600)
        if r.returncode != 0:
            shutil.rmtree(ws, ignore_errors=True)
            return {"error": f"scaffold failed: {r.stderr.strip()[:300]}"}

    # Defence in depth on top of the isolated HOME: bind an empty directory over the checkout so
    # the repository is not merely unreferenced but unreachable. Two rounds of "found the leak"
    # were wrong -- first the skill symlinks, then the project records -- while runs kept walking
    # in through something else (`ls /home/james`, then a remembered path). A fence that does not
    # depend on having enumerated every source of the path is worth its ~1ms. bwrap is optional:
    # without it the run proceeds and `repo_leaks()` still refuses to score a run that got in.
    sandbox = []
    if shutil.which("bwrap"):
        empty = tempfile.mkdtemp(prefix="agyeval-norepo-")   # outside ws: never seen by the agent
        sandbox = ["bwrap", "--dev-bind", "/", "/", "--ro-bind", empty, REPO, "--"]

    # `agy` bounds a turn by wall clock only -- it has no equivalent of max_turns, so a case's
    # `max_turns:` is honoured by `claude plugin eval` and ignored here. timeout_seconds is the
    # only budget that bites on this runner, which is why it has to be per case.
    cmd = sandbox + ["agy", "--output-format", "stream-json", "--dangerously-skip-permissions",
                     "--print-timeout", f"{timeout}s", f"--print={prompt}"]
    # Without --agent, agy runs its default persona and none of this repo's AGENT.md files are
    # loaded -- verified by asking a loaded session whether a read-only persona was in effect
    # ("NONE" without the flag, the ast-treesitter mandate with it). A case that omits `agent:`
    # therefore tests the skills only, which is what every case did before this was added.
    if fm.get("agent"):
        cmd += ["--agent", fm["agent"]]
    if fm.get("model"):
        cmd += ["--model", fm["model"]]
    env = dict(os.environ)
    if isolate:
        env["HOME"] = isolated_home(ws)
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=ws, capture_output=True, text=True, timeout=timeout + 120,
                              env=env)
    except subprocess.TimeoutExpired:
        if not keep:
            shutil.rmtree(ws, ignore_errors=True)
        return {"error": f"agy exceeded {timeout + 120}s"}

    out = parse_trace(proc.stdout)
    out["elapsed"] = time.time() - started
    out["workspace"] = ws
    if out.get("error") is None and proc.returncode != 0:
        out["error"] = f"agy exit {proc.returncode}: {proc.stderr.strip()[:300]}"
    # `agy --print-timeout` ends the turn cleanly: exit 0, a result event, and no response. The
    # trace is intact, so tool_used graders still pass while every last_message grader reports
    # "pattern NOT found" -- a truncated run reads exactly like a misbehaving agent. It is not
    # gradeable, so say so. (First agent-case run: 4 of 12 runs were this, and the report
    # blamed 13 assertions.)
    if out.get("error") is None and not (out.get("last_message") or "").strip():
        out["error"] = (f"no final message after {out['elapsed']:.0f}s of a {timeout}s turn "
                        f"({len(out['tools'])} tool calls): the turn was cut off, so assertions "
                        f"on last_message cannot be graded. Raise the case's timeout_seconds.")
    leaks = repo_leaks(out["tools"])
    if out.get("error") is None and leaks:
        out["error"] = (f"run reached the repo checkout ({len(leaks)} call(s), first: "
                        f"{leaks[0][:120]!r}) -- it can read its own graders, so this run is "
                        f"not gradeable in either direction")
    # Record created files before the workspace goes away.
    out["files"] = sorted(
        os.path.relpath(os.path.join(dp, f), ws)
        for dp, dn, fn in os.walk(ws) for f in fn
        if ".git" not in dp and ".ast-graph" not in dp
    )
    if not keep:
        shutil.rmtree(ws, ignore_errors=True)
    return out


def parse_trace(stdout):
    """stream-json -> {tools: [(name, arg)], last_message, usage, status}."""
    tools, text, result, init, seen = [], [], {}, {}, set()
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        ev = d.get("event")
        if ev == "init":
            init = d.get("init") or {}
        elif ev == "result":
            result = d.get("result") or {}
        elif ev == "step_update":
            su = d.get("step_update") or {}
            if su.get("text_delta"):
                text.append(su["text_delta"])
            if su.get("step_type") == "tool":
                ti = su.get("tool_info") or {}
                name = ti.get("name")
                # step_update repeats per state transition; one entry per (step, tool).
                key = (su.get("step_index"), name)
                if name and key not in seen:
                    seen.add(key)
                    params = ti.get("parameters") or {}
                    arg = next((str(params[k]) for k in TOOL_ARG_KEYS if k in params),
                               json.dumps(params))
                    tools.append((name, arg))
    return {
        "tools": tools,
        "last_message": result.get("response") or "".join(text),
        "usage": result.get("usage") or {},
        "status": result.get("status"),
        "num_turns": result.get("num_turns"),
        "model": init.get("model"),
        "error": None if result else "no result event in trace",
    }


# ---------------------------------------------------------------- grading
def target_text(g, run):
    t = g.get("target", "last_message")
    if t == "last_message":
        return run["last_message"]
    if t == "trace":
        return "\n".join(f"{n} {a}" for n, a in run["tools"])
    if t == "files":
        return "\n".join(run["files"])
    return run["last_message"]


def matches_tool(wanted, actual):
    return actual == wanted or actual in TOOL_ALIASES.get(wanted, set())


def grade(g, run, judge):
    """Return (passed, note). passed is None when the grader could not be scored."""
    gt = g.get("type")
    if gt == "tool_used":
        want = g["tool"]
        hits = [a for n, a in run["tools"] if matches_tool(want, n)]
        if g.get("input_match"):
            rx = re.compile(g["input_match"])
            hits = [a for a in hits if rx.search(a)]
        lo, hi = g.get("min", 1), g.get("max")
        ok = len(hits) >= lo and (hi is None or len(hits) <= hi)
        return ok, f"{len(hits)} matching call(s)" + (f"; e.g. {hits[0][:70]}" if hits else "")
    if gt == "regex":
        flags = 0
        for ch in str(g.get("flags", "")):
            flags |= {"i": re.I, "m": re.M, "s": re.S}.get(ch, 0)
        found = bool(re.search(g["pattern"], target_text(g, run), flags))
        mode = g.get("match", "contains")
        if mode == "not_contains":
            return (not found), f"pattern {'found' if found else 'absent'}"
        if str(mode).startswith("count:"):
            want = int(str(mode).split(":", 1)[1])
            n = len(re.findall(g["pattern"], target_text(g, run), flags))
            return n == want, f"{n} occurrence(s), wanted {want}"
        return found, f"pattern {'found' if found else 'NOT found'}"
    if gt == "tool_order":
        names = [n for n, _ in run["tools"]]
        bi = next((i for i, n in enumerate(names) if matches_tool(g["before"], n)), None)
        ai = next((i for i, n in enumerate(names) if matches_tool(g["after"], n)), None)
        if bi is None or ai is None:
            return False, "one of the tools was never called"
        return bi < ai, f"indices {bi} then {ai}"
    if gt == "file_exists":
        hit = fnmatch.filter(run["files"], g["path"])
        return bool(hit), f"{len(hit)} match(es)"
    if gt == "llm":
        if not judge:
            return None, "llm grader skipped (--no-judge)"
        ok, note = judge_llm(g, run)
        if ok is None:
            # Retry ONLY when no verdict came back at all (empty response, timeout, garbled
            # output) -- that is a transport failure, not an answer. Never retry a FAIL: asking
            # again until the judge agrees with you is not grading, it is fishing.
            ok, note = judge_llm(g, run)
            note = f"{note} (after retry)"
        return ok, note
    return None, f"unsupported grader type {gt!r}"


JUDGE_TEMPLATE = """You are grading one assertion about an AI coding agent's behavior.

CRITERIA:
{criteria}

MATERIAL UNDER TEST:
<<<
{material}
>>>

Reply with exactly one word on the first line: PASS or FAIL.
On the second line give a one-sentence reason. Output nothing else."""


def judge_llm(g, run):
    material = target_text(g, run)[:12000]
    prompt = JUDGE_TEMPLATE.format(criteria=g["criteria"].strip(), material=material)
    try:
        r = subprocess.run(
            ["agy", "--output-format", "json", "--print-timeout", "180s", f"--print={prompt}"],
            cwd=tempfile.gettempdir(), capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return None, "judge timed out"
    body = r.stdout.strip()
    try:
        d = json.loads(body)
        if isinstance(d, dict):
            # `--output-format json` puts `response` at the top level; tolerate a nested
            # `result.response` too, in case that changes between agy versions.
            nested = (d.get("result") or {}).get("response")
            body = d["response"] if "response" in d else (nested if nested is not None else body)
    except ValueError:
        pass
    body = body.strip()
    if not body:
        return None, "judge returned nothing"
    # Scan for a standalone verdict rather than trusting line 1: a global instruction file
    # (see ~/.gemini/GEMINI.md) can prepend a preamble ahead of the answer.
    m = re.search(r"\b(PASS|FAIL)\b", body, re.I)
    if not m:
        return None, f"judge gave no verdict: {body[:100]!r}"
    reason = body[m.end():].strip().splitlines()
    reason = reason[0].strip()[:110] if reason else ""
    return m.group(1).upper() == "PASS", f"judge: {reason}"


# ---------------------------------------------------------------- driver
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case", action="append", help="glob of case names (repeatable)")
    ap.add_argument("--runs", type=int, help="override the per-case runs:")
    ap.add_argument("--timeout", type=int, help="per-turn seconds, overriding each case's "
                                                "timeout_seconds (default: the case's value, else 420)")
    ap.add_argument("--no-judge", action="store_true", help="skip llm graders instead of spending a judge call")
    ap.add_argument("--keep-temp", action="store_true", help="leave workspaces on disk for inspection")
    ap.add_argument("--use-global-context", action="store_true",
                    help="load the operator's ~/.gemini/GEMINI.md too (default: isolated, so the "
                         "run measures the skills rather than personal instructions)")
    ap.add_argument("--json", dest="json_out", help="write structured results here")
    args = ap.parse_args()

    if shutil.which("agy") is None:
        print("agy not found on PATH -- this runner needs the Antigravity CLI", file=sys.stderr)
        return 2

    cases = sorted(d for d in os.listdir(HERE)
                   if os.path.isdir(os.path.join(HERE, d))
                   and d not in {"fixtures", "scaffold", "results", "__pycache__"})
    if args.case:
        cases = [c for c in cases if any(fnmatch.fnmatch(c, p) for p in args.case)]
    if not cases:
        print("no matching cases", file=sys.stderr)
        return 1

    gemini_md = os.path.expanduser("~/.gemini/GEMINI.md")
    if os.path.exists(gemini_md) and args.use_global_context:
        print(f"note: --use-global-context is set, so {gemini_md} is loaded. Personal instructions "
              f"add behaviour (preambles, extra files, extra verification steps) that is not "
              f"attributable to the skills under test.\n")

    report, failed = {"cases": []}, 0
    for case in cases:
        cdir = os.path.join(HERE, case)
        fm, prompt = load_frontmatter(os.path.join(cdir, "prompt.md"))
        graders = []
        gdir = os.path.join(cdir, "graders")
        for gf in sorted(globmod.glob(os.path.join(gdir, "*.md"))):
            gspec, _ = load_frontmatter(gf)
            graders.append((os.path.basename(gf), gspec))
        runs = args.runs or fm.get("runs", 3)
        print(f"== {case}  ({runs} run(s), {len(graders)} grader(s))")
        crec = {"name": case, "runs": []}

        for i in range(1, runs + 1):
            # `timeout_seconds:` was read by validate.py and documented in the README, but the
            # runner passed args.timeout unconditionally -- so every case ran on the 420s default
            # however long it declared, which is what cut the helm and kubernetes cases off.
            timeout = args.timeout or fm.get("timeout_seconds", 420)
            run = run_case_once(cdir, fm, prompt, timeout, args.keep_temp,
                                isolate=not args.use_global_context)
            if run.get("error"):
                print(f"  run {i}: ERROR {run['error']}")
                crec["runs"].append({"run": i, "error": run["error"]})
                failed += 1
                continue
            # The tool list is kept, not just its length: when a grader fails, the trace is the
            # evidence, and re-running to recover it costs another turn's tokens.
            rrec = {"run": i, "graders": [], "usage": run["usage"],
                    "elapsed": round(run["elapsed"], 1), "tool_count": len(run["tools"]),
                    "tools": [{"name": n, "arg": a[:300]} for n, a in run["tools"]],
                    "last_message": run["last_message"][:4000]}
            line = []
            for name, gspec in graders:
                ok, note = grade(gspec, run, judge=not args.no_judge)
                mark = {True: "ok", False: "FAIL", None: "skip"}[ok]
                if ok is False:
                    failed += 1
                line.append(f"{mark} {name.removesuffix('.md')}")
                rrec["graders"].append({"name": name, "type": gspec.get("type"),
                                        "passed": ok, "note": note})
            tok = run["usage"].get("total_tokens", 0)
            print(f"  run {i}: {'  '.join(line)}   [{len(run['tools'])} tools, {run['elapsed']:.0f}s, {tok:,} tok]")
            for g in rrec["graders"]:
                if g["passed"] is not True:
                    print(f"          {g['name']}: {g['note']}")
            crec["runs"].append(rrec)
        report["cases"].append(crec)

    report["failed_assertions"] = failed
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(f"\nwrote {args.json_out}")
    print(f"\n{len(cases)} case(s), {failed} failed assertion(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
