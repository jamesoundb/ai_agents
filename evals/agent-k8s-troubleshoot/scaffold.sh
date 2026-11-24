#!/usr/bin/env bash
# Workspace for this case: the k8s-incident fixture, then cluster-snapshot.sh.
set -euo pipefail
s="$(dirname "${BASH_SOURCE[0]}")/../scaffold"
"$s/lay-fixture.sh" k8s-incident
exec "$s/cluster-snapshot.sh"
