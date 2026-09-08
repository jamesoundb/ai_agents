#!/usr/bin/env bash
# Thin wrapper: find this skill's physical location (it is usually a symlink into the agents repo)
# and run the repository's install.sh with the same arguments.
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd -P)"
REPO="$(cd "$HERE/../../.." && pwd -P)"
if [ ! -x "$REPO/install.sh" ]; then
  echo "install-agents: cannot find install.sh above $HERE. This skill must live inside (or be symlinked" >&2
  echo "from) a clone of the agents repository; clone it and run this skill from that checkout." >&2
  exit 2
fi
exec "$REPO/install.sh" "$@"
