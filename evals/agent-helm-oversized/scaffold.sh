#!/usr/bin/env bash
# Workspace for this case: the helm-api fixture, then prepare.sh.
set -euo pipefail
s="$(dirname "${BASH_SOURCE[0]}")/../scaffold"
"$s/lay-fixture.sh" helm-api
exec "$s/prepare.sh"
