#!/usr/bin/env bash
# Workspace for this case: the k8s-guardrails fixture, then prepare.sh.
set -euo pipefail
s="$(dirname "${BASH_SOURCE[0]}")/../scaffold"
"$s/lay-fixture.sh" k8s-guardrails
exec "$s/prepare.sh"
