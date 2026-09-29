#!/usr/bin/env bash
# Makes the code-graph engine runnable inside an eval workspace.
#
# Eval runs get a fresh HOME, so the tree-sitter venv that run.sh caches at
# ~/.cache/astgraph/venv is not there and run.sh would pip-install it on every run
# (~30s, needs network). Pre-warm it once here instead.
#
# To skip the install entirely, point ASTGRAPH_VENV at a venv that already has the
# deps before invoking `claude plugin eval`, e.g.
#   ASTGRAPH_VENV="$HOME/.cache/astgraph/venv" claude plugin eval .
set -euo pipefail
REPO="${EVAL_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
RUN="$REPO/skills/code-graph/scripts/run.sh"
[ -x "$RUN" ] || { echo "scaffold: engine launcher not found at $RUN" >&2; exit 2; }
# One trivial invocation forces the dependency check/install and warms the venv.
printf 'x = 1\n' > /tmp/astgraph-warm.py
"$RUN" skeleton /tmp/astgraph-warm.py --no-stats >/dev/null
rm -f /tmp/astgraph-warm.py
echo "scaffold: code-graph engine ready"
