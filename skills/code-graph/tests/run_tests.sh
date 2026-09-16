#!/usr/bin/env bash
# run_tests.sh — engine regression tests against tests/fixture (copied to a temp dir; never writes
# into the repo). Uses the same Python discovery as scripts/run.sh (installs the venv if needed).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$HERE/../scripts/run.sh" --help >/dev/null 2>&1 || true   # ensures the venv exists
VENV="${ASTGRAPH_VENV:-$HOME/.cache/astgraph/venv}"
has_deps() { "$1" -c "import tree_sitter, tree_sitter_language_pack" >/dev/null 2>&1; }
PY=""
for cand in "${ASTGRAPH_PYTHON:-}" "$VENV/bin/python" python3 python; do
  [ -n "$cand" ] || continue
  if command -v "$cand" >/dev/null 2>&1 && has_deps "$cand"; then PY="$cand"; break; fi
done
[ -n "$PY" ] || { echo "no Python with tree-sitter found" >&2; exit 2; }
exec "$PY" -B "$HERE/test_engine.py" "$@"
