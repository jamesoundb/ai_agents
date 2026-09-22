#!/usr/bin/env bash
# release_notes.sh — print Markdown release notes for a tag: the commits since the previous tag,
# plus how to install or roll back to this exact version.
#
#   tools/ci/release_notes.sh v1.2.0 > release-notes.md
#
# Needs full history and tags (in CI: GIT_DEPTH: 0). The first tag lists every commit.
set -euo pipefail
TAG="${1:?usage: release_notes.sh TAG}"
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null || { echo "tag $TAG not found" >&2; exit 2; }

# Previous version tag reachable from this one (ignore anything that is not vX.Y.Z[-suffix]).
PREV="$(git describe --tags --abbrev=0 --match 'v[0-9]*' "$TAG^" 2>/dev/null || true)"
RANGE="${PREV:+$PREV..}$TAG"

echo "## What changed"
echo
if [ -n "$PREV" ]; then echo "Changes since \`$PREV\`:"; else echo "First release. All changes:"; fi
echo
# No merge commits: an MR's own commits already describe the change.
# shellcheck disable=SC2016  # backticks are literal Markdown, not command substitution
git log --no-merges --format='- %s (`%h`)' "$RANGE"
echo
# Which agents and skills this release touches, so developers know what to re-test.
TOUCHED="$(git diff --name-only ${PREV:+"$PREV"} "$TAG" -- agents skills 2>/dev/null \
  | awk -F/ 'NF>2 {print $1"/"$2}' | sort -u)"
if [ -n "$PREV" ] && [ -n "$TOUCHED" ]; then
  echo "### Agents and skills changed"
  echo
  # shellcheck disable=SC2016  # literal Markdown backticks
  printf '%s\n' "$TOUCHED" | sed 's/^/- `/; s/$/`/'
  echo
fi
cat <<EOF
## Install or pin this version

\`\`\`bash
cd ~/ai_agents && git fetch --tags && git checkout $TAG
./install.sh --harness all --scope user
\`\`\`

Go back to the latest version with \`git checkout main && git pull\`, then re-run the install.
EOF
