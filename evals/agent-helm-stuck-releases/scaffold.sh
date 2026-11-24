#!/usr/bin/env bash
# Workspace for this case: the helm-releases fixture, then cluster-snapshot.sh.
set -euo pipefail
s="$(dirname "${BASH_SOURCE[0]}")/../scaffold"
"$s/lay-fixture.sh" helm-releases
exec "$s/cluster-snapshot.sh"
