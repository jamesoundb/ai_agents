#!/usr/bin/env bash
# run.sh — repeatable launcher for astgraph.py.
# Finds a Python that has tree-sitter + tree-sitter-language-pack; if none, creates a private
# venv under ~/.cache/astgraph and installs them there (one-time, ~30s; with uv when it is on PATH,
# otherwise venv + pip). Writes nothing into the repo except the graph under .ast-graph/ when
# `build` is run.
# `run.sh --setup` does that now and also downloads the grammar of every language the engine
# parses (normally fetched on first parse), so later runs only read ~/.cache. install.sh runs it;
# a sandboxed session (no writes outside the project, no network) depends on it having run.
# Requires Python 3.10+: the tree-sitter and tree-sitter-language-pack releases pinned in
# requirements.txt need it, and the engine is tested on 3.12.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/astgraph.py"
VENV="${ASTGRAPH_VENV:-$HOME/.cache/astgraph/venv}"
MIN_PY="3.10"

has_deps() { "$1" -c "import tree_sitter, tree_sitter_language_pack" >/dev/null 2>&1; }
# py_ok PY: true when PY is at least MIN_PY (empty/unparseable output counts as too old).
py_ok() { "$1" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >/dev/null 2>&1; }
py_ver() { "$1" -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>/dev/null || echo "unknown"; }
# ensure_venv BASE: create $VENV from BASE if it does not exist, then install requirements.txt into
# it. An existing venv is extended, never recreated (other skills share it). uv, when present,
# seeds pip into the venv so a skill without uv can still extend it.
ensure_venv() {
  if [ ! -x "$VENV/bin/python" ]; then
    mkdir -p "$(dirname "$VENV")" 2>/dev/null || return 1
    if command -v uv >/dev/null 2>&1; then uv venv --quiet --seed --python "$1" "$VENV" || return 1
    else "$1" -m venv "$VENV" && "$VENV/bin/pip" install --quiet --upgrade pip >/dev/null || return 1; fi
  fi
  if command -v uv >/dev/null 2>&1; then uv pip install --quiet --python "$VENV/bin/python" -r "$HERE/requirements.txt"
  else "$VENV/bin/pip" install --quiet -r "$HERE/requirements.txt"; fi
}
cannot_setup() {
  echo "astgraph: cannot set up $VENV here (no write access or no network: a sandboxed session?)." >&2
  echo "astgraph: run once outside the sandbox: \"$HERE/run.sh\" --setup (install.sh runs it)" >&2
  exit 2
}

SETUP=0; [ "${1:-}" = "--setup" ] && SETUP=1

PY=""
for cand in "${ASTGRAPH_PYTHON:-}" "$VENV/bin/python" python3 python; do
  [ -n "$cand" ] || continue
  if command -v "$cand" >/dev/null 2>&1 && has_deps "$cand"; then PY="$cand"; break; fi
done

if [ -z "$PY" ]; then
  # No interpreter has the dependencies yet: create the venv from the base python3, which must
  # meet the floor (an ASTGRAPH_PYTHON that lacks the deps is used as the base when given). uv
  # can instead find or download a suitable interpreter itself.
  BASE="${ASTGRAPH_PYTHON:-python3}"
  if ! command -v "$BASE" >/dev/null 2>&1 || ! py_ok "$BASE"; then
    if command -v uv >/dev/null 2>&1 && [ -z "${ASTGRAPH_PYTHON:-}" ]; then BASE=">=$MIN_PY"
    elif ! command -v "$BASE" >/dev/null 2>&1; then
      echo "astgraph: '$BASE' not found; install Python $MIN_PY+ (tested on 3.12)" >&2; exit 2
    else
      echo "astgraph: $BASE is Python $(py_ver "$BASE"); the engine needs Python $MIN_PY+ (tested on 3.12)." >&2
      echo "astgraph: point ASTGRAPH_PYTHON at a newer interpreter, install one, or install uv." >&2
      exit 2
    fi
  fi
  echo "astgraph: installing tree-sitter into $VENV (one-time)" >&2
  if ! { ensure_venv "$BASE" >&2 && has_deps "$VENV/bin/python"; }; then cannot_setup; fi
  PY="$VENV/bin/python"
fi

if [ "$SETUP" = 1 ]; then
  # Grammars of every language EXT_LANG maps to; download() skips the ones already present.
  # Older packs bundle their grammars in the wheel and have no download(). Packs before 1.21 have
  # a download() that fetches nothing (upstream #201) and an existing venv is never upgraded, so
  # each grammar is then loaded: get_language() fetches a missing one in every release.
  "$PY" -B -c '
import sys
sys.path.insert(0, sys.argv[1])
from astgraph import EXT_LANG
import tree_sitter_language_pack as t
langs = sorted(set(EXT_LANG.values()))
if hasattr(t, "download"):
    t.download(langs)
missing = []
for lang in langs:
    try:
        t.get_language(lang)
    except Exception:
        missing.append(lang)
if missing:
    sys.exit("astgraph: grammars not downloaded: " + " ".join(missing))
print("astgraph: ready: %s, grammars: %s" % (sys.executable, " ".join(langs)), file=sys.stderr)
' "$HERE" || cannot_setup
  exit 0
fi

exec "$PY" -B "$SCRIPT" "$@"
