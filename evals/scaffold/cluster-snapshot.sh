#!/usr/bin/env bash
# Puts the cluster-graph test snapshot (a sanitized `kubegraph snapshot --save` of a minikube cluster
# with planted failures) into the eval workspace as cluster-snapshot/: the agent troubleshoots a
# cluster it cannot reach, from the state a teammate saved. Also warms the code-graph venv.
set -euo pipefail
REPO="${EVAL_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cp -r "$REPO/skills/cluster-graph/tests/fixture" cluster-snapshot
"$(dirname "${BASH_SOURCE[0]}")/prepare.sh"
echo "scaffold: cluster snapshot copied ($(ls cluster-snapshot | wc -l) files)"
