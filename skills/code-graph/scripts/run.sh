#!/usr/bin/env bash
# run.sh — repeatable launcher for astgraph.py.
# Finds a Python that has tree-sitter + tree-sitter-language-pack; if none, creates a private
# venv under ~/.cache/astgraph and installs them there (one-time, ~30s). Never touches the repo.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/astgraph.py"
VENV="${ASTGRAPH_VENV:-$HOME/.cache/astgraph/venv}"

has_deps() { "$1" -c "import tree_sitter, tree_sitter_language_pack" >/dev/null 2>&1; }

PY=""
for cand in "${ASTGRAPH_PYTHON:-}" "$VENV/bin/python" python3 python; do
  [ -n "$cand" ] || continue
  if command -v "$cand" >/dev/null 2>&1 && has_deps "$cand"; then PY="$cand"; break; fi
done

if [ -z "$PY" ]; then
  echo "astgraph: installing tree-sitter into $VENV (one-time)" >&2
  mkdir -p "$(dirname "$VENV")"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip >/dev/null
  "$VENV/bin/pip" install --quiet -r "$HERE/requirements.txt"
  PY="$VENV/bin/python"
  has_deps "$PY" || { echo "astgraph: dependency install failed" >&2; exit 2; }
fi

exec "$PY" -B "$SCRIPT" "$@"
