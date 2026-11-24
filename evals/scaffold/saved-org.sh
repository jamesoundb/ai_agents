#!/usr/bin/env bash
# For dungeon-map cases: the test Gitea org's saved API responses (a `build --save` of
# skills/dungeon-map/tests/gitea.sh's throwaway instance) as org-snapshot/, as a teammate would share
# them. The server is not running, so only `build --from` can read it.
set -euo pipefail
REPO="${EVAL_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
mkdir -p org-snapshot
cp "$REPO/skills/dungeon-map/tests/fixture/gitea/responses.json" org-snapshot/
echo "scaffold: org-snapshot/responses.json copied"
