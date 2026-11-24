#!/usr/bin/env bash
# Workspace for this case: the tf-gke fixture, then prepare.sh.
set -euo pipefail
s="$(dirname "${BASH_SOURCE[0]}")/../scaffold"
"$s/lay-fixture.sh" tf-gke
exec "$s/prepare.sh"
