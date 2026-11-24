#!/usr/bin/env bash
# Workspace for this case: the teamcity-red-build fixture, then prepare.sh.
set -euo pipefail
s="$(dirname "${BASH_SOURCE[0]}")/../scaffold"
"$s/lay-fixture.sh" teamcity-red-build
exec "$s/prepare.sh"
