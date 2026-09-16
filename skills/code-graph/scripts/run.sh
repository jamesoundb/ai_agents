#!/usr/bin/env bash
# run.sh — repeatable launcher for astgraph.py.
# Finds a Python that has tree-sitter + tree-sitter-language-pack; if none, creates a private
# venv under ~/.cache/astgraph and installs them there (one-time, ~30s). Writes nothing into the
# repo except the graph under .ast-graph/ when `build` is run.
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

PY=""
for cand in "${ASTGRAPH_PYTHON:-}" "$VENV/bin/python" python3 python; do
  [ -n "$cand" ] || continue
  if command -v "$cand" >/dev/null 2>&1 && has_deps "$cand"; then PY="$cand"; break; fi
done

if [ -z "$PY" ]; then
  # No interpreter has the dependencies yet: create the venv from the base python3, which must
  # meet the floor (an ASTGRAPH_PYTHON that lacks the deps is used as the base when given).
  BASE="${ASTGRAPH_PYTHON:-python3}"
  command -v "$BASE" >/dev/null 2>&1 || { echo "astgraph: '$BASE' not found; install Python $MIN_PY+ (tested on 3.12)" >&2; exit 2; }
  if ! py_ok "$BASE"; then
    echo "astgraph: $BASE is Python $(py_ver "$BASE"); the engine needs Python $MIN_PY+ (tested on 3.12)." >&2
    echo "astgraph: point ASTGRAPH_PYTHON at a newer interpreter or install one." >&2
    exit 2
  fi
  echo "astgraph: installing tree-sitter into $VENV (one-time)" >&2
  mkdir -p "$(dirname "$VENV")"
  "$BASE" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip >/dev/null
  "$VENV/bin/pip" install --quiet -r "$HERE/requirements.txt"
  PY="$VENV/bin/python"
  has_deps "$PY" || { echo "astgraph: dependency install failed" >&2; exit 2; }
fi

exec "$PY" -B "$SCRIPT" "$@"
