#!/usr/bin/env bash
# Workspace for this case: saved-org.sh.
set -euo pipefail
s="$(dirname "${BASH_SOURCE[0]}")/../scaffold"
exec "$s/saved-org.sh"
