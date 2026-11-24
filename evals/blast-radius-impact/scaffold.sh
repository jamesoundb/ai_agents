#!/usr/bin/env bash
# Workspace for this case: the orders fixture, then prepare.sh.
set -euo pipefail
s="$(dirname "${BASH_SOURCE[0]}")/../scaffold"
"$s/lay-fixture.sh" orders
exec "$s/prepare.sh"
