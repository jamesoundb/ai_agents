#!/usr/bin/env bash
# Workspace for this case: the k8s-net fixture, then cluster-snapshot.sh.
set -euo pipefail
s="$(dirname "${BASH_SOURCE[0]}")/../scaffold"
"$s/lay-fixture.sh" k8s-net
exec "$s/cluster-snapshot.sh"
