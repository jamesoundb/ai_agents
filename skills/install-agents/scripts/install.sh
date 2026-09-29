#!/usr/bin/env bash
# Thin wrapper: find this skill's physical location (it is usually a symlink into the agents repo)
# and run the repository's install.sh with the same arguments.
set -euo pipefail

# Resolve a path through any chain of symlinks, portably. `readlink -f` is a GNU extension:
# macOS shipped a readlink without it until recently, and under `set -e` inside a command
# substitution its failure kills this script -- which is the first thing a fresh session runs.
# Only plain `readlink` (BSD and GNU) is used here, so it works on bash 3.2 too.
resolve_link() {
  local target="$1" dir
  while [ -L "$target" ]; do
    dir="$(dirname "$target")"
    target="$(readlink "$target")"
    case "$target" in /*) ;; *) target="$dir/$target" ;; esac
  done
  printf '%s\n' "$target"
}
HERE="$(cd "$(dirname "$(resolve_link "${BASH_SOURCE[0]}")")" && pwd -P)"
REPO="$(cd "$HERE/../../.." && pwd -P)"
if [ ! -x "$REPO/install.sh" ]; then
  echo "install-agents: cannot find install.sh above $HERE. This skill must live inside (or be symlinked" >&2
  echo "from) a clone of the agents repository; clone it and run this skill from that checkout." >&2
  exit 2
fi
exec "$REPO/install.sh" "$@"
