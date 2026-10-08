#!/usr/bin/env bash
# run.sh -- launcher for kubegraph.py (stdlib only): picks a Python 3.10+ and runs it. Read-only
# against the cluster; the snapshot goes to ~/.cache/kubegraph/<context>.db (KUBEGRAPH_DIR).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=""
for cand in "${KUBEGRAPH_PYTHON:-}" python3 python; do
  [ -n "$cand" ] || continue
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    PY="$cand"; break
  fi
done
[ -n "$PY" ] || { echo "kubegraph: needs Python 3.10+ (set KUBEGRAPH_PYTHON)" >&2; exit 2; }
exec "$PY" -B "$HERE/kubegraph.py" "$@"
