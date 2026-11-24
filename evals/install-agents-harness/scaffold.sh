#!/usr/bin/env bash
# Workspace for this case: clone-repo.sh.
set -euo pipefail
s="$(dirname "${BASH_SOURCE[0]}")/../scaffold"
exec "$s/clone-repo.sh"
