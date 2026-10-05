#!/usr/bin/env bash
# Session-start hook: build (or refresh) code graphs in the background, so the first question does
# not spend a turn on `build`. Prints one line of context for the model. Queries wait for a build
# that is still running (engine build lock).
#
#   autobuild.sh                  Claude Code / Gemini CLI SessionStart hook: the session directory
#                                 comes from CLAUDE_PROJECT_DIR / GEMINI_PROJECT_DIR; the context is
#                                 printed as hookSpecificOutput.additionalContext
#   autobuild.sh --antigravity    Antigravity SessionStart hook: the workspace folders come from
#                                 `workspacePaths` in the JSON on stdin; the context is printed as an
#                                 injectSteps ephemeralMessage
#   ASTGRAPH_AUTOBUILD=0          disable without uninstalling
#   ASTGRAPH_AUTOBUILD_MAX=8      most repositories built per session
#
# What it builds, for each session/workspace directory:
#   - inside a git checkout: that checkout, at its top level;
#   - not a checkout (a problem directory holding several cloned repositories): every git checkout
#     directly below it (hidden directories skipped);
# one graph per repository, in parallel with the CPU cores shared out. Never $HOME or /. Each graph
# directory ignores itself, so nothing shows up in `git status`. Never fails the session: every
# problem exits 0 without output.
set -u
[ "${ASTGRAPH_AUTOBUILD:-1}" = 0 ] && exit 0
format=claude
[ "${1:-}" = --antigravity ] && format=antigravity
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
max="${ASTGRAPH_AUTOBUILD_MAX:-8}"

dirs=()
if [ "$format" = antigravity ]; then
  # {"workspacePaths": ["/path", ...], ...}; the hook's working directory is the config folder
  while IFS= read -r d; do [ -n "$d" ] && dirs+=("$d"); done < <(python3 -c \
    'import json, sys; print("\n".join(json.load(sys.stdin).get("workspacePaths") or []))' 2>/dev/null)
else
  dirs+=("${GEMINI_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-$PWD}}")
fi
[ "${#dirs[@]}" -gt 0 ] || exit 0

repos=()
skipped=0
single=""     # the session sits inside exactly this checkout (one-repository wording)
add_repo() {  # add_repo <checkout top>: once each, never / or $HOME, at most $max
  local r="$1" x
  if [ "$r" = "/" ] || [ "$r" = "${HOME:-}" ]; then return 0; fi
  for x in ${repos[@]+"${repos[@]}"}; do [ "$x" = "$r" ] && return 0; done
  if [ "${#repos[@]}" -lt "$max" ]; then repos+=("$r"); else skipped=$((skipped + 1)); fi
}
for d in "${dirs[@]}"; do
  [ -d "$d" ] || continue
  d="$(cd "$d" && pwd -P)"
  top="$(git -C "$d" rev-parse --show-toplevel 2>/dev/null || true)"
  if [ -n "$top" ]; then
    add_repo "$top"; single="$top"
    continue
  fi
  if [ "$d" = "/" ] || [ "$d" = "${HOME:-}" ]; then continue; fi
  for c in "$d"/*/; do
    c="${c%/}"
    [ -d "$c" ] || continue
    # a checkout whose top level is this directory itself (not a plain folder inside another checkout)
    [ "$(git -C "$c" rev-parse --show-toplevel 2>/dev/null)" = "$(cd "$c" && pwd -P)" ] && add_repo "$(cd "$c" && pwd -P)"
  done
done
[ "${#repos[@]}" -gt 0 ] || exit 0

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

if [ "${#repos[@]}" = 1 ] && [ "$single" = "${repos[0]}" ]; then
  start_build "${repos[0]}" || exit 0
  msg="Code graph: being built in the background at .ast-graph/ in the repository root (code-graph skill). Do not run build: queries wait for it and keep it current."
else
  cpus="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"
  per_repo=$(( cpus / ${#repos[@]} )); [ "$per_repo" -ge 1 ] || per_repo=1
  names=()
  for r in "${repos[@]}"; do
    start_build "$r" "$per_repo" && names+=("$r")
  done
  [ "${#names[@]}" -gt 0 ] || exit 0
  msg="Code graphs: being built in the background, one per repository: ${names[*]} (code-graph skill). Query one with run.sh query --root <repository> <question> (or run queries from inside it). Do not run build: queries wait for it and keep it current."
  [ "$skipped" -gt 0 ] && msg="$msg $skipped more repositories were not built (limit ASTGRAPH_AUTOBUILD_MAX=$max); build those when needed."
fi

if [ "$format" = antigravity ]; then
  printf '{"injectSteps":[{"ephemeralMessage":"%s"}]}\n' "$(json_escape "$msg")"
else
  printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "$(json_escape "$msg")"
fi
