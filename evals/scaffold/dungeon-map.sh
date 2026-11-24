#!/usr/bin/env bash
# For compass cases: an ai-task workspace in the eval workspace, and a dungeon map of the test Gitea
# org (skills/dungeon-map/tests/fixture/gitea, replayed: no API call) in the isolated HOME that
# run_agy.py gives the agent (<workspace>/home). The scaffold itself runs under the real HOME, so
# the cache path is set explicitly and the operator's ~/.cache/dungeon-map is never touched.
set -euo pipefail
REPO="${EVAL_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
"$REPO/skills/ai-task/scripts/ai-task" init >/dev/null
"$REPO/skills/dungeon-map/scripts/run.sh" --cache "$PWD/home/.cache/dungeon-map" build localhost:3300/dmtest \
  --provider gitea --from "$REPO/skills/dungeon-map/tests/fixture/gitea" >/dev/null
echo "scaffold: ai-task workspace and dungeon map ready"
