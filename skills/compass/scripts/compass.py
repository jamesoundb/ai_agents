#!/usr/bin/env python3
"""compass.py -- find the repositories a problem needs in the dungeon map, and hand them to ai-task.

  compass.py status                                  # which maps exist, how old they are
  compass.py find payments refund [--topic T] [--language L]
  compass.py show payments-api                       # metadata, depends on, used by
  compass.py plan "refunds fail for card payments"   # proposed repos + reasons + next commands
  compass.py handoff payments-api proto --slug fix-refunds
  compass.py seed payments-api                       # map facts into .ai/instructions/reference/<repo>.md

Reads the graphs that ../../dungeon-map builds in ~/.cache/dungeon-map (DUNGEONMAP_DIR); never
calls a provider API. The workspace is the nearest parent of the working directory (or
--workspace) that contains .ai/, as for ai-task.
"""
import argparse
import math
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "dungeon-map", "scripts"))
import dungeonmap as dm  # noqa: E402  (sibling skill, by relative path)

DM_RUN = os.path.normpath(os.path.join(HERE, "..", "..", "dungeon-map", "scripts", "run.sh"))
RUN = os.path.join(HERE, "run.sh")
MAX_AGE_DAYS = 7
BEGIN = "<!-- BEGIN compass: facts from the dungeon map; `compass seed` rewrites this block -->"
END = "<!-- END compass -->"
STOP = set("""a an and are as at be but by can do does for from has have how i if in into is it its
it's me my no not of on or our so than that the their them then there these they this to too up us
was we were what when where which while who why will with would you your should could need needs
make makes fix fixes fixed add adds change changes update updates using use used work works broken
bug issue problem error errors fails fail failing also all any some new old get gets like want""".split())
WEIGHTS = {"name": 4.0, "topics": 3.0, "description": 2.0, "path": 1.5, "readme": 1.0, "language": 1.0}
VIA_TEXT = {
    "submodule": "git submodule", "npm": "npm dependency", "go": "Go module", "python": "Python dependency",
    "maven": "Maven dependency", "gradle": "Gradle dependency", "helm": "Helm chart dependency",
    "terraform": "Terraform module", "ci-include": "CI include", "ci-trigger": "CI downstream trigger",
    "ci-component": "CI component", "workflow": "reusable workflow", "action": "CI action",
    "image": "container base image",
}


def shown(path):
    """A path as the agent should type it: relative when under the working directory."""
    rel = os.path.relpath(path)
    return rel if not rel.startswith("..") else path


def tokens(text):
    out = []
    for w in re.findall(r"[A-Za-z][A-Za-z0-9]*|[0-9]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")):
        w = w.lower()
        if len(w) > 1 and w not in STOP:
            out.append(w)
    return out


def same_word(a, b):
    """Exact, or a shared stem of 5+ letters (deploy / deployment, payment / payments)."""
    if a == b:
        return True
    n = min(len(a), len(b))
    return n >= 5 and a[:max(5, n - 2)] == b[:max(5, n - 2)]


def fields(r):
    return {
        "name": tokens(r["name"]), "path": tokens(r["path"].rsplit("/", 1)[0]),
        "topics": [t for x in r["topics"] for t in tokens(x)], "description": tokens(r["description"]),
        "readme": tokens(r["readme"]), "language": tokens(r["language"]),
    }


def rank(m, query, keys=None, include_archived=False):
    """Score repositories against the query words: field weight x inverse document frequency."""
    keys = keys if keys is not None else list(m.repos)
    words = list(dict.fromkeys(tokens(query)))
    if not words:
        return [], words
    fx = {k: fields(m.repos[k]) for k in keys}
    n = len(keys) or 1
    df = {w: sum(1 for k in keys if any(same_word(w, t) for f in fx[k].values() for t in f)) for w in words}
    scored = []
    for k in keys:
        r = m.repos[k]
        if r["archived"] and not include_archived:
            continue
        score, hits = 0.0, []
        for w in words:
            if not df[w]:
                continue
            best = max(((WEIGHTS[f], f) for f, toks in fx[k].items() if any(same_word(w, t) for t in toks)), default=None)
            if best:
                score += best[0] * math.log(1 + n / df[w])
                hits.append(f"{w} ({best[1]})")
        if score:
            if r["fork"]:
                score *= 0.5
            scored.append((round(score, 1), k, hits))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return scored, words


def short(m, key):
    """The shortest name that is unique in the map: path inside its host, or host/path."""
    r = m.repos[key]
    return r["path"] if len(m.resolve(r["path"])) == 1 else key


def flags(r):
    f = []
    if r["archived"]:
        f.append("archived")
    if r["fork"]:
        f.append("fork")
    if (r.get("note") or "").startswith("error"):
        f.append("not scanned")
    return f" [{', '.join(f)}]" if f else ""


def edge_text(m, e, out=True):
    other = short(m, e["dst"] if out else e["src"])
    where = f"{e['file']}:{e['line']}" if e["line"] else e["file"]
    side = "" if out else f" in {short(m, e['src'])}"
    guess = ", name match" if e["match"] == "prefix" else ""
    return f"{other} ({VIA_TEXT.get(e['via'], e['via'])}: {where}{side}{guess})"


def load(a):
    m = dm.load_map(a.cache, hosts=set(a.host) if a.host else None)
    if not m.graphs:
        print(f"compass: no dungeon map in {a.cache or dm.CACHE}. Build one first (one per org or group):\n"
              f"  {shown(DM_RUN)} build https://github.com/ORG\n"
              f"  {shown(DM_RUN)} build https://gitlab.example.com/GROUP --provider gitlab")
        return None
    return m


def stale_lines(m, max_days):
    out = []
    for gk, meta in sorted(m.graphs.items()):
        t = dm.parse_time(meta["built_at"])
        if t and (dm.now() - t).days >= max_days:
            out.append(f"stale: {gk} was built {dm.age_text(meta['built_at'])}; refresh it (only changed repositories "
                       f"are rescanned): {shown(DM_RUN)} build {meta['host']}/{meta['org']} --provider {meta['provider']}")
    return out


def need_one(m, name):
    hits = m.resolve(name)
    if len(hits) == 1:
        return hits[0]
    if not hits:
        sys.stderr.write(f"compass: no repository '{name}' in the map (try: compass find {name})\n")
    else:
        sys.stderr.write(f"compass: '{name}' is ambiguous: {', '.join(sorted(hits))}\n")
    return None


# --------------------------------------------------------------------------------- workspace
def find_workspace(start=None):
    d = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.isdir(os.path.join(d, ".ai")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def clone_spec(r, ssh=False):
    """What `ai-task clone` takes: owner/name for github.com (it assumes GitHub), else the URL."""
    if ssh and r["clone_ssh"]:
        return r["clone_ssh"]
    if r["host"].lower() == "github.com" and r["path"].count("/") == 1:
        return r["path"]
    return r["clone_https"] or (r["web_url"].rstrip("/") + ".git")


def clone_name(spec):
    """The directory ai-task clone creates: basename of the spec without .git."""
    return os.path.basename(spec.rstrip("/")).removesuffix(".git")


def origin_ident(path):
    try:
        url = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"], capture_output=True,
                             text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return dm.git_ident(url)


def workspace_state(m, root, keys, ssh=False):
    """[(key, state, name, spec)], state: have | missing | collision (dir holds another repository)."""
    out = []
    for k in keys:
        r = m.repos[k]
        spec = clone_spec(r, ssh)
        name = clone_name(spec)
        d = os.path.join(root, name)
        if not os.path.exists(os.path.join(d, ".git")):
            out.append((k, "missing", name, spec))
            continue
        want = {dm.git_ident(r["clone_https"]), dm.git_ident(r["clone_ssh"])} - {None}
        out.append((k, "have" if origin_ident(d) in want else "collision", name, spec))
    return out


def print_handoff(m, root, keys, slug, ssh, cwd_root):
    """Workspace section and the next commands, one per line (the agent runs one per call)."""
    st = workspace_state(m, root, keys, ssh)
    ai = ".ai/bin/ai-task" if cwd_root else os.path.join(root, ".ai", "bin", "ai-task")
    print(f"\nworkspace {root}:")
    for k, s, name, _spec in st:
        if s == "have":
            print(f"  have     {short(m, k)} -> {name}/")
        elif s == "missing":
            print(f"  missing  {short(m, k)}")
        else:
            print(f"  CLASH    {short(m, k)}: {name}/ already holds another repository "
                  f"({origin_ident(os.path.join(root, name))}); ai-task clones by basename, so clone this one "
                  f"by hand under another name or work in a separate workspace")
    missing = [x for x in st if x[1] == "missing"]
    names = [name for _, s, name, _ in st if s != "collision"]
    print("\nnext (one command per call):")
    if not cwd_root:
        print(f"  (run these from the workspace root {root})")
    for _, _, _, spec in missing:
        print(f"  {ai} clone {spec}")
    if missing:
        run = shown(RUN) if cwd_root else RUN  # the commands run from the root, so no cwd-relative path
        print(f"  {run} seed {' '.join(short(m, k) for k, *_ in missing)}")
    print(f"  {ai} start {slug or '<slug>'} {' '.join(names)}")
    if not slug:
        print("  (<slug>: the problem's .ai/instructions/<slug>.md; write it first if it does not exist)")


# --------------------------------------------------------------------------------- commands
def cmd_status(a):
    m = load(a)
    if not m:
        return 1
    for gk, meta in sorted(m.graphs.items()):
        print(f"{gk}: {meta['repos']} repos ({meta['provider']}), built {meta['built_at']} ({dm.age_text(meta['built_at'])})")
    print(f"{len(m.repos)} repositories, {len(m.edges)} cross-repository edges")
    for line in stale_lines(m, a.max_age_days):
        print(line)
    return 0


def cmd_find(a):
    m = load(a)
    if not m:
        return 1
    keys = [k for k, r in m.repos.items()
            if (not a.topic or any(t.lower() == a.topic.lower() for t in r["topics"]))
            and (not a.language or r["language"].lower() == a.language.lower())]
    if a.terms:
        scored, _ = rank(m, " ".join(a.terms), keys, include_archived=True)
    else:
        scored = [(0, k, []) for k in sorted(keys)]
    if not scored:
        print("no repository matches")
        return 1
    for score, k, hits in scored[:a.max]:
        r = m.repos[k]
        lang = f" {r['language']}" if r["language"] else ""
        print(f"{short(m, k)}{flags(r)}{lang} -- {r['description'] or r['readme'][:100] or '(no description)'}")
        if hits:
            print(f"    score {score}: {', '.join(hits)}")
    if len(scored) > a.max:
        print(f"... {len(scored) - a.max} more (--max)")
    for line in stale_lines(m, a.max_age_days):
        print(line)
    return 0


def cmd_show(a):
    m = load(a)
    if not m:
        return 1
    rc = 0
    for name in a.repos:
        k = need_one(m, name)
        if not k:
            rc = 1
            continue
        r = m.repos[k]
        print(f"{k}{flags(r)}")
        print(f"  {r['web_url']}  default branch {r['default_branch'] or '-'}, last activity {r['last_activity'] or '-'}")
        if r["description"]:
            print(f"  description: {r['description']}")
        if r["language"] or r["topics"]:
            print(f"  language: {r['language'] or '-'}; topics: {', '.join(r['topics']) or '-'}")
        if r["readme"]:
            print(f"  readme: {r['readme']}")
        out, inc = m.out_edges(k), m.in_edges(k)
        print(f"  depends on ({len(out)}):" + ("" if out else " nothing in the map"))
        for e in out:
            print(f"    {edge_text(m, e, True)}")
        print(f"  used by ({len(inc)}):" + ("" if inc else " nothing in the map"))
        for e in inc:
            print(f"    {edge_text(m, e, False)}")
        if r.get("note"):
            print(f"  note: {r['note']}")
    for line in stale_lines(m, a.max_age_days):
        print(line)
    return rc


def cmd_plan(a):
    m = load(a)
    if not m:
        return 1
    scored, words = rank(m, a.problem)
    print(f"map: {len(m.repos)} repositories in {len(m.graphs)} graphs; words: {', '.join(words) or '(none)'}")
    for line in stale_lines(m, a.max_age_days):
        print(line)
    if not scored:
        archived = [k for _, k, _ in rank(m, a.problem, include_archived=True)[0] if m.repos[k]["archived"]]
        if archived:
            print("only archived repositories match: " + ", ".join(short(m, k) for k in archived[:5])
                  + "; name one with compass handoff if the work really is there")
        else:
            print("no repository matches the problem's words; try compass find with other words, or name the repository")
        return 1
    top = scored[0][0]
    seeds = [s for s in scored if s[0] >= top * a.cutoff][:a.max]
    print("\nproposed (clone these):")
    for score, k, hits in seeds:
        r = m.repos[k]
        print(f"  {short(m, k)}{flags(r)} -- {r['description'] or r['readme'][:100]}")
        print(f"      why: matches {', '.join(hits)} (score {score})")
    chosen = {k for _, k, _ in seeds}
    related = {}
    for k in chosen:
        for e in m.out_edges(k):
            if e["dst"] not in chosen:
                related.setdefault(e["dst"], []).append(f"{short(m, k)} depends on it: {edge_text(m, e, True).split(' (', 1)[1][:-1]}")
        for e in m.in_edges(k):
            if e["src"] not in chosen:
                related.setdefault(e["src"], []).append(f"it depends on {short(m, k)}: {edge_text(m, e, False).split(' (', 1)[1][:-1]}")
    if related:
        print("\nrelated (add one only if the change crosses into it: an interface, a shared type, a version bump):")
        for k in sorted(related, key=lambda x: short(m, x)):
            print(f"  {short(m, k)}{flags(m.repos[k])}")
            for why in related[k]:
                print(f"      {why}")
    others = [s for s in scored if s[1] not in chosen][:3]
    if others:
        print("\nalso matched (weaker): " + ", ".join(f"{short(m, k)} ({s})" for s, k, _ in others))
    root = find_workspace(a.workspace)
    if root:
        print_handoff(m, root, [k for _, k, _ in seeds], a.slug, a.ssh, os.path.abspath(os.getcwd()) == root)
    else:
        print("\nno ai-task workspace here (.ai/ in this or a parent directory); create one with the ai-task skill's init, "
              "then: compass handoff REPO...")
    return 0


def cmd_handoff(a):
    m = load(a)
    if not m:
        return 1
    keys = []
    for name in a.repos:
        k = need_one(m, name)
        if not k:
            return 1
        keys.append(k)
    root = find_workspace(a.workspace)
    if not root:
        print("compass: no ai-task workspace (.ai/) in the working directory or a parent; run the ai-task skill's init first")
        return 1
    for line in stale_lines(m, a.max_age_days):
        print(line)
    print_handoff(m, root, list(dict.fromkeys(keys)), a.slug, a.ssh, os.path.abspath(os.getcwd()) == root)
    return 0


def reference_block(m, k):
    r = m.repos[k]
    meta = m.graphs[r["graph"]]
    lines = [BEGIN, f"## From the dungeon map ({r['graph']}, built {meta['built_at'][:10]})", "",
             f"- Repository: {r['path']} ({r['web_url']}), default branch `{r['default_branch'] or '-'}`{flags(r)}"]
    if r["description"]:
        lines.append(f"- Description: {r['description']}")
    if r["language"] or r["topics"]:
        lines.append(f"- Language: {r['language'] or '-'}; topics: {', '.join(r['topics']) or '-'}")
    if r["readme"]:
        lines.append(f"- README: {r['readme']}")
    out, inc = m.out_edges(k), m.in_edges(k)
    lines.append("- Depends on: " + ("; ".join(edge_text(m, e, True) for e in out) if out else "nothing in the map"))
    lines.append("- Used by: " + ("; ".join(edge_text(m, e, False) for e in inc) if inc else "nothing in the map"))
    lines.append(END)
    return "\n".join(lines) + "\n"


def cmd_seed(a):
    m = load(a)
    if not m:
        return 1
    root = find_workspace(a.workspace)
    if not root:
        print("compass: no ai-task workspace (.ai/) in the working directory or a parent")
        return 1
    refdir = os.path.join(root, ".ai", "instructions", "reference")
    os.makedirs(refdir, exist_ok=True)
    rc = 0
    for name in a.repos:
        k = need_one(m, name)
        if not k:
            rc = 1
            continue
        fname = clone_name(clone_spec(m.repos[k], a.ssh))
        p = os.path.join(refdir, f"{fname}.md")
        # Same header as ai-task's ensure_reference, when the clone has not created the file yet.
        text = open(p, encoding="utf-8").read() if os.path.exists(p) else \
            f"# {fname}\n\nLiving reference: architecture, setup, gotchas. Not a worklog.\n"
        block = reference_block(m, k)
        if BEGIN in text and END in text:
            pre, rest = text.split(BEGIN, 1)
            text = pre + block + rest.split(END, 1)[1].lstrip("\n")
        else:
            text = text.rstrip("\n") + "\n\n" + block
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"seeded {shown(p)} from {short(m, k)}")
    return rc


def main(argv=None):
    ap = argparse.ArgumentParser(prog="compass.py", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # --cache works before the command (as in dungeon-map) or after it; SUPPRESS keeps the
    # subcommand's default from overwriting a value given before the command.
    ap.add_argument("--cache", default=None, help=f"dungeon-map cache (default {dm.CACHE})")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cache", default=argparse.SUPPRESS, help=f"dungeon-map cache (default {dm.CACHE})")
    common.add_argument("--host", action="append", help="only graphs of this host (repeatable)")
    common.add_argument("--max-age-days", type=int, default=MAX_AGE_DAYS, help="call a graph stale after N days (default 7)")
    ws = argparse.ArgumentParser(add_help=False)
    ws.add_argument("--workspace", help="the ai-task workspace (default: nearest parent with .ai/)")
    ws.add_argument("--ssh", action="store_true", help="clone over ssh instead of https")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", parents=[common], help="maps in the cache and their age")
    f = sub.add_parser("find", parents=[common], help="repositories by keyword, topic or language")
    f.add_argument("terms", nargs="*")
    f.add_argument("--topic")
    f.add_argument("--language")
    f.add_argument("--max", type=int, default=15)
    s = sub.add_parser("show", parents=[common], help="a repository: metadata, depends on, used by")
    s.add_argument("repos", nargs="+")
    p = sub.add_parser("plan", parents=[common, ws], help="the repositories a problem needs, and the next commands")
    p.add_argument("problem")
    p.add_argument("--max", type=int, default=3, help="most repositories proposed (default 3)")
    p.add_argument("--cutoff", type=float, default=0.5, help="propose repos scoring at least this share of the best (0.5)")
    p.add_argument("--slug", help="ai-task slug for the start command")
    h = sub.add_parser("handoff", parents=[common, ws], help="clone commands for these repositories, then ai-task start")
    h.add_argument("repos", nargs="+")
    h.add_argument("--slug")
    sd = sub.add_parser("seed", parents=[common, ws], help="write map facts into the repositories' reference files")
    sd.add_argument("repos", nargs="+")
    a = ap.parse_args(argv)
    return {"status": cmd_status, "find": cmd_find, "show": cmd_show, "plan": cmd_plan,
            "handoff": cmd_handoff, "seed": cmd_seed}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
