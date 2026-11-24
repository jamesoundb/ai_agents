#!/usr/bin/env bash
# run.sh -- launcher for dungeonmap.py (stdlib only): picks a Python 3.10+ and runs it. Reads the
# provider API through gh / glab / gt; the map goes to ~/.cache/dungeon-map/ (DUNGEONMAP_DIR).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=""
for cand in "${DUNGEONMAP_PYTHON:-}" python3 python; do
  [ -n "$cand" ] || continue
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    PY="$cand"; break
  fi
done
[ -n "$PY" ] || { echo "dungeon-map: needs Python 3.10+ (set DUNGEONMAP_PYTHON)" >&2; exit 2; }
exec "$PY" -B "$HERE/dungeonmap.py" "$@"
