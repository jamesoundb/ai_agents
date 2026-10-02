#!/usr/bin/env bash
# Seeds the eval workspace with this repository's tracked files (working-tree contents), so the
# committed install-agents bootstrap (.agents/skills/install-agents) is discovered as a
# workspace skill exactly as in a fresh clone. evals/ is left out: it holds the graders.
set -euo pipefail
REPO="${EVAL_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
(cd "$REPO" && git ls-files -z | grep -zv '^evals/' | tar --null -T - -cf -) | tar -xf -
test -f .agents/skills/install-agents/SKILL.md || { echo "scaffold: bootstrap skill missing" >&2; exit 2; }
echo "scaffold: repository copied ($(git -C "$REPO" rev-parse --short HEAD) + working tree)"
