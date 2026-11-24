#!/usr/bin/env bash
# For dungeon-map cases: the test Gitea org's saved API responses (a `build --save` of
# skills/dungeon-map/tests/gitea.sh's throwaway instance) as org-snapshot/, as a teammate would share
# them. The server is not running, so only `build --from` can read it.
set -euo pipefail
# shellcheck source=/dev/null  # scaffold/repo-root.sh, linted on its own
. "$(dirname "${BASH_SOURCE[0]}")/repo-root.sh"
mkdir -p org-snapshot
cp "$REPO/skills/dungeon-map/tests/fixture/gitea/responses.json" org-snapshot/
echo "scaffold: org-snapshot/responses.json copied"
