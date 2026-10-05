#!/usr/bin/env bash
# Session-start hook: build (or refresh) code graphs in the background, so the first question does
# not spend a turn on `build`. Prints one line of context for the model. Queries wait for a build
# that is still running (engine build lock).
#
#   autobuild.sh                  hook command (stdin: the hook's JSON, unused)
#   ASTGRAPH_AUTOBUILD=0          disable without uninstalling
#   ASTGRAPH_AUTOBUILD_MAX=8      most repositories built for a multi-repo session directory
#
# Where it builds:
#   - session inside a git checkout: that checkout, at its top level;
#   - session in a directory that is not a checkout (a problem directory holding several cloned
#     repositories): every git checkout directly below it (hidden directories skipped, at most
#     ASTGRAPH_AUTOBUILD_MAX), in parallel with the CPU cores shared out, one graph per repository.
# Never $HOME or /. Each graph directory ignores itself, so nothing shows up in `git status`.
# Never fails the session: every problem exits 0 without output.
set -u
[ "${ASTGRAPH_AUTOBUILD:-1}" = 0 ] && exit 0
dir="${CLAUDE_PROJECT_DIR:-$PWD}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -d "$dir" ] || exit 0
dir="$(cd "$dir" && pwd -P)"

json_escape() {  # the context line goes into a JSON string
  local s="$1"
  s="${s//\\/\\\\}"; s="${s//\"/\\\"}"; s="${s//$'\n'/ }"; s="${s//$'\t'/ }"
  printf '%s' "$s"
}

start_build() {  # start_build <repo top> [jobs]: background build, logged inside the graph directory
  mkdir -p "$1/.ast-graph" 2>/dev/null && [ -w "$1/.ast-graph" ] || return 1
  local jobs=()
  [ -n "${2:-}" ] && jobs=(--jobs "$2")   # parallel builds share the cores out
  # The launcher writes its own pid into the build lock and then execs run.sh, which execs the engine
  # (same pid): the lock exists before the engine starts, so a query arriving that early already waits.
  # shellcheck disable=SC2016  # $$ and "$@" belong to the inner sh
  nohup sh -c 'echo $$ > "$1"; shift; exec "$@"' sh "$1/.ast-graph/graph.db.lock" \
    "$here/run.sh" build --root "$1" ${jobs[@]+"${jobs[@]}"} > "$1/.ast-graph/autobuild.log" 2>&1 < /dev/null &
}

top="$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null || true)"
if [ -n "$top" ]; then
  if [ "$top" = "/" ] || [ "$top" = "${HOME:-}" ]; then exit 0; fi
  start_build "$top" || exit 0
  msg="Code graph: being built in the background at .ast-graph/ in the repository root (code-graph skill). Do not run build: queries wait for it and keep it current."
else
  if [ "$dir" = "/" ] || [ "$dir" = "${HOME:-}" ]; then exit 0; fi
  max="${ASTGRAPH_AUTOBUILD_MAX:-8}"
  repos=()
  skipped=0
  for d in "$dir"/*/; do
    d="${d%/}"
    [ -d "$d" ] || continue
    # a checkout whose top level is this directory itself (not a plain folder inside another checkout)
    [ "$(git -C "$d" rev-parse --show-toplevel 2>/dev/null)" = "$(cd "$d" && pwd -P)" ] || continue
    if [ "${#repos[@]}" -lt "$max" ]; then repos+=("$d"); else skipped=$((skipped + 1)); fi
  done
  [ "${#repos[@]}" -gt 0 ] || exit 0
  cpus="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"
  per_repo=$(( cpus / ${#repos[@]} )); [ "$per_repo" -ge 1 ] || per_repo=1
  names=()
  for r in "${repos[@]}"; do
    start_build "$r" "$per_repo" && names+=("$(basename "$r")/")
  done
  [ "${#names[@]}" -gt 0 ] || exit 0
  msg="Code graphs: being built in the background, one per repository: ${names[*]} (code-graph skill). Query one with run.sh query --root <repository> <question> (or run queries from inside it). Do not run build: queries wait for it and keep it current."
  [ "$skipped" -gt 0 ] && msg="$msg $skipped more repositories were not built (limit ASTGRAPH_AUTOBUILD_MAX=$max); build those when needed."
fi
printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "$(json_escape "$msg")"
