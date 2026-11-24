#!/usr/bin/env bash
# Copies evals/fixtures/<name> into the eval workspace (the current directory), the way each case
# starts. `claude plugin eval` grants a case's add_dirs read-only instead of copying them, and only
# from inside the case directory, so the fixture is laid in by the case's scaffold.sh instead.
set -euo pipefail
# shellcheck source=/dev/null  # scaffold/repo-root.sh, linted on its own
. "$(dirname "${BASH_SOURCE[0]}")/repo-root.sh"
name="${1:?usage: lay-fixture.sh <fixture name under evals/fixtures>}"
src="$REPO/evals/fixtures/$name"
[ -d "$src" ] || { echo "scaffold: no fixture $src" >&2; exit 2; }
# .ast-graph is generated; a workspace never starts with one.
(cd "$src" && tar --exclude=.ast-graph -cf - .) | tar -xf -
echo "scaffold: fixture $name laid in"
