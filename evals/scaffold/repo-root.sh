# shellcheck shell=bash disable=SC2034  # sourced: REPO is read by the script that sources this
# Sourced by the scaffold scripts: sets REPO to the checkout the eval runs from.
#
# The runners reach the scaffold in different ways, so the repository is found in this order:
#   1. EVAL_REPO_ROOT    -- run_agy.py sets it.
#   2. .repo-root here   -- run_evals.py --harness claude writes it: `claude plugin eval` runs the
#                           scaffold from a built plugin copy and passes it no environment at all.
#   3. ../.. from here   -- evals/ inside the checkout itself.
_scaffold_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "${EVAL_REPO_ROOT:-}" ]; then
  REPO="$EVAL_REPO_ROOT"
elif [ -f "$_scaffold_dir/.repo-root" ]; then
  REPO="$(cat "$_scaffold_dir/.repo-root")"
else
  REPO="$(cd "$_scaffold_dir/../.." && pwd)"
fi
