#!/usr/bin/env bash
# run.sh — repeatable launcher for rightsize.py.
# Finds a Python that has PyYAML; if none, installs it into the private venv shared by the skills
# (~/.cache/astgraph/venv), creating the venv first if needed (one-time, a few seconds; with uv when
# it is on PATH, otherwise venv + pip). An existing venv is extended, never recreated, so the other
# skills' tree-sitter install is kept. Never touches the repo.
# `run.sh --setup` does that now, so later runs only read ~/.cache: install.sh runs it, and a
# sandboxed session (no writes outside the project, no network) depends on it having run.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/rightsize.py"
VENV="${ASTGRAPH_VENV:-$HOME/.cache/astgraph/venv}"

has_deps() { "$1" -c "import yaml" >/dev/null 2>&1; }
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
  echo "rightsize: cannot set up $VENV here (no write access or no network: a sandboxed session?)." >&2
  echo "rightsize: run once outside the sandbox: \"$HERE/run.sh\" --setup (install.sh runs it)" >&2
  exit 2
}

SETUP=0; [ "${1:-}" = "--setup" ] && SETUP=1

PY=""
for cand in "${ASTGRAPH_PYTHON:-}" "$VENV/bin/python" python3 python; do
  [ -n "$cand" ] || continue
  if command -v "$cand" >/dev/null 2>&1 && has_deps "$cand"; then PY="$cand"; break; fi
done

if [ -z "$PY" ]; then
  # without python3, uv finds or downloads a suitable interpreter itself
  BASE=python3
  if ! command -v python3 >/dev/null 2>&1 && command -v uv >/dev/null 2>&1; then BASE=">=3.10"; fi
  echo "rightsize: installing PyYAML into $VENV (one-time)" >&2
  if ! { ensure_venv "$BASE" >&2 && has_deps "$VENV/bin/python"; }; then cannot_setup; fi
  PY="$VENV/bin/python"
fi

if [ "$SETUP" = 1 ]; then echo "rightsize: ready: $PY" >&2; exit 0; fi

exec "$PY" -B "$SCRIPT" "$@"
