#!/usr/bin/env bash
# run.sh — repeatable launcher for tfreview.py.
# Finds a Python that has tree-sitter + tree-sitter-language-pack; if none, creates a private
# venv under ~/.cache/astgraph and installs them there (one-time, ~30s; with uv when it is
# on PATH, otherwise venv + pip). Never touches the repo.
# `run.sh --setup` does that now (and downloads the grammar it parses), so later runs only read ~/.cache: install.sh runs it, and a
# sandboxed session (no writes outside the project, no network) depends on it having run.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/tfreview.py"
VENV="${ASTGRAPH_VENV:-$HOME/.cache/astgraph/venv}"

has_deps() { "$1" -c "import tree_sitter, tree_sitter_language_pack" >/dev/null 2>&1; }
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
  echo "tfreview: cannot set up $VENV here (no write access or no network: a sandboxed session?)." >&2
  echo "tfreview: run once outside the sandbox: \"$HERE/run.sh\" --setup (install.sh runs it)" >&2
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
  echo "tfreview: installing tree-sitter into $VENV (one-time)" >&2
  if ! { ensure_venv "$BASE" >&2 && has_deps "$VENV/bin/python"; }; then cannot_setup; fi
  PY="$VENV/bin/python"
fi

if [ "$SETUP" = 1 ]; then
  # the hcl grammar now (normally fetched on first parse); older packs bundle it and have no download().
  # Packs before 1.21 have a download() that fetches nothing (upstream #201): get_language() fetches it.
  "$PY" -B -c '
import sys, tree_sitter_language_pack as t
if hasattr(t, "download"):
    t.download(["hcl"])
try:
    t.get_language("hcl")
except Exception:
    sys.exit(1)
' || cannot_setup
  echo "tfreview: ready: $PY, grammar: hcl" >&2
  exit 0
fi

exec "$PY" -B "$SCRIPT" "$@"
