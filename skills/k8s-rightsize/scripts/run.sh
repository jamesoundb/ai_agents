#!/usr/bin/env bash
# run.sh — repeatable launcher for rightsize.py.
# Finds a Python that has PyYAML; if none, installs it into the private venv shared by the skills
# (~/.cache/astgraph/venv), creating the venv first if needed (one-time, a few seconds). An existing
# venv is extended, never recreated, so the other skills' tree-sitter install is kept.
# Never touches the repo.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/rightsize.py"
VENV="${ASTGRAPH_VENV:-$HOME/.cache/astgraph/venv}"

has_deps() { "$1" -c "import yaml" >/dev/null 2>&1; }

PY=""
for cand in "${ASTGRAPH_PYTHON:-}" "$VENV/bin/python" python3 python; do
  [ -n "$cand" ] || continue
  if command -v "$cand" >/dev/null 2>&1 && has_deps "$cand"; then PY="$cand"; break; fi
done

if [ -z "$PY" ]; then
  if [ ! -x "$VENV/bin/python" ]; then
    echo "rightsize: creating $VENV (one-time)" >&2
    mkdir -p "$(dirname "$VENV")"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip >/dev/null
  fi
  echo "rightsize: installing PyYAML into $VENV (one-time)" >&2
  "$VENV/bin/pip" install --quiet -r "$HERE/requirements.txt"
  PY="$VENV/bin/python"
  has_deps "$PY" || { echo "rightsize: dependency install failed" >&2; exit 2; }
fi

exec "$PY" -B "$SCRIPT" "$@"
