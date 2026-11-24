#!/usr/bin/env bash
# Makes the code-graph engine runnable inside an eval workspace.
#
# Eval runs get a fresh HOME, so the tree-sitter venv that run.sh caches at
# ~/.cache/astgraph/venv is not there and run.sh would pip-install it on every run
# (~30s, needs network). Pre-warm it once here instead.
#
# run_agy.py links ~/.cache/astgraph into the run's HOME, so this is instant there;
# `claude plugin eval` passes the run no ASTGRAPH_VENV, so there it builds the venv.
set -euo pipefail
# shellcheck source=/dev/null  # scaffold/repo-root.sh, linted on its own
. "$(dirname "${BASH_SOURCE[0]}")/repo-root.sh"
RUN="$REPO/skills/code-graph/scripts/run.sh"
[ -x "$RUN" ] || { echo "scaffold: engine launcher not found at $RUN" >&2; exit 2; }
# --setup builds the venv and downloads every grammar the engines load (yaml, hcl, ...), as
# install.sh does. Warming with one Python file fetched only the Python grammar: under
# `claude plugin eval` the run's ~/.cache is read-only and the network closed, so
# k8s-manifest-review then failed to parse every manifest (COR000, 0 objects reviewed).
# Seed the run's HOME from the operator's own caches (venv + grammars, ~135 MB, copied in well under
# a second): built from the network on every run, 2 of 3 parallel runs failed on a DNS lookup
# (2026-11-24). run_agy.py links ~/.cache/astgraph in already; anything present is left alone.
real_home="$(python3 -c 'import os, pwd; print(pwd.getpwuid(os.getuid()).pw_dir)')"
if [ "$real_home" != "$HOME" ]; then
  for c in astgraph tree-sitter-language-pack; do
    if [ ! -e "$HOME/.cache/$c" ] && [ -d "$real_home/.cache/$c" ]; then
      mkdir -p "$HOME/.cache" && cp -a "$real_home/.cache/$c" "$HOME/.cache/$c"
    fi
  done
fi
"$RUN" --setup >/dev/null
echo "scaffold: code-graph engine and grammars ready"
