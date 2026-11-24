#!/usr/bin/env bash
# Workspace for this case: the python-billing fixture, then prepare.sh.
set -euo pipefail
s="$(dirname "${BASH_SOURCE[0]}")/../scaffold"
"$s/lay-fixture.sh" python-billing
exec "$s/prepare.sh"
