#!/usr/bin/env bash
# Workspace for this case: the py-hub fixture, then prepare.sh.
set -euo pipefail
s="$(dirname "${BASH_SOURCE[0]}")/../scaffold"
"$s/lay-fixture.sh" py-hub
exec "$s/prepare.sh"
