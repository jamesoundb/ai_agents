#!/usr/bin/env bash
# Workspace for this case: dungeon-map.sh.
set -euo pipefail
s="$(dirname "${BASH_SOURCE[0]}")/../scaffold"
exec "$s/dungeon-map.sh"
