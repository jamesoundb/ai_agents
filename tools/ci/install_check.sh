#!/usr/bin/env bash
# install_check.sh — install every agent and skill into throwaway locations and verify the result.
# Runs the two documented installs:
#   1. project scope into a temp git repo      (install.sh --harness all --target DIR)
#   2. user scope into a temp HOME              (install.sh --harness all --scope user)
# then uninstalls the project install and checks the repo is left clean. Never touches the real
# HOME or this checkout. Exit 0 = all checks passed.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail=0
check() {  # check "description" test-args...
  local desc="$1"; shift
  if "$@"; then echo "  ok   $desc"; else echo "  FAIL $desc"; fail=1; fi
}
count() { find "$1" -mindepth 1 -maxdepth 1 2>/dev/null | wc -l | tr -d ' '; }
run_install() {  # run_install LOG args...: a failing install.sh prints its output and stops the check
  local log="$1"; shift
  if ! "$@" > "$log" 2>&1; then
    echo "  FAIL install.sh $* exited non-zero; output:"; sed 's/^/       /' "$log" | tail -n 20
    exit 1
  fi
}

# Expected counts come from the repo itself, so adding a skill or agent needs no edit here.
N_AGENTS="$(count "$REPO/agents")"
N_SKILLS="$(count "$REPO/skills")"
N_SKILLS_NO_BOOT=$((N_SKILLS - 1))  # install-agents is excluded from targets (committed bootstrap)
echo "repo: $N_AGENTS agents, $N_SKILLS skills"
BEFORE="$(git -C "$REPO" status --porcelain)"   # compared at the end: installs must not touch the checkout

echo "== command line =="
"$REPO/install.sh" --help > "$WORK/help.txt"
check "--help lists the options"             grep -q '^  --target DIR' "$WORK/help.txt"
check "--help prints no script code"         test -z "$(grep -E 'set -euo|REPO=|^usage\(\)' "$WORK/help.txt")"
# shellcheck disable=SC2016  # single quotes are intended: $0/$1 expand inside bash -c
check "missing --target gives a clear error" bash -c '! "$0" --harness claude --target "$1/nope" 2>&1 | grep -q "No such file" && ! "$0" --harness claude --target "$1/nope" >/dev/null 2>&1' "$REPO/install.sh" "$WORK"
mkdir -p "$WORK/scope-test"
# shellcheck disable=SC2016  # single quotes are intended: $0/$1 expand inside bash -c
check "unknown --scope is rejected"          bash -c '! "$0" --harness claude --scope global --target "$1" >/dev/null 2>&1' "$REPO/install.sh" "$WORK/scope-test"
check "rejected --scope wrote nothing"       test -z "$(ls -A "$WORK/scope-test")"

echo "== project scope =="
PROJ="$WORK/project"
mkdir -p "$PROJ" && git -C "$PROJ" init -q
run_install "$WORK/project.log" "$REPO/install.sh" --harness all --target "$PROJ"
check "claude agents = $N_AGENTS"            test "$(count "$PROJ/.claude/agents")" -eq "$N_AGENTS"
check "claude skills = $N_SKILLS_NO_BOOT"    test "$(count "$PROJ/.claude/skills")" -eq "$N_SKILLS_NO_BOOT"
check "copilot agents = $N_AGENTS"           test "$(count "$PROJ/.github/agents")" -eq "$N_AGENTS"
check "antigravity agents = $N_AGENTS"       test "$(count "$PROJ/.agents/agents")" -eq "$N_AGENTS"
check "AGENTS.md has the managed block"      grep -q 'BEGIN managed by install.sh' "$PROJ/AGENTS.md"
check "CLAUDE.md imports AGENTS.md"          grep -qx '@AGENTS.md' "$PROJ/CLAUDE.md"
check "skill symlinks resolve"               test -f "$PROJ/.claude/skills/code-graph/SKILL.md"

run_install "$WORK/uninstall.log" "$REPO/install.sh" --harness all --target "$PROJ" --uninstall
check "uninstall leaves the project clean"   test -z "$(git -C "$PROJ" status --porcelain)"

echo "== user scope (temp HOME) =="
FAKE_HOME="$WORK/home"
mkdir -p "$FAKE_HOME"
HOME="$FAKE_HOME" run_install "$WORK/user.log" "$REPO/install.sh" --harness all --scope user
check "HOME/.claude/agents = $N_AGENTS"          test "$(count "$FAKE_HOME/.claude/agents")" -eq "$N_AGENTS"
check "HOME/.claude/skills = $N_SKILLS_NO_BOOT"  test "$(count "$FAKE_HOME/.claude/skills")" -eq "$N_SKILLS_NO_BOOT"
check "HOME/.copilot/agents = $N_AGENTS"         test "$(count "$FAKE_HOME/.copilot/agents")" -eq "$N_AGENTS"
check "HOME/.gemini/config/agents = $N_AGENTS"   test "$(count "$FAKE_HOME/.gemini/config/agents")" -eq "$N_AGENTS"
check "no AGENTS.md written into HOME"        test ! -e "$FAKE_HOME/AGENTS.md"
check "rendered agents are non-empty"         test -s "$FAKE_HOME/.claude/agents/terraform.md"

echo "== a developer's own files are never destroyed =="
OWN="$WORK/own"
mkdir -p "$OWN/.claude/skills/code-graph" "$OWN/.claude/agents"
echo "THEIRS" > "$OWN/.claude/skills/code-graph/SKILL.md"
echo "THEIRS" > "$OWN/.claude/agents/terraform.md"
HOME="$OWN" "$REPO/install.sh" --harness claude --scope user > "$WORK/clash.log" 2>&1 && clash_rc=0 || clash_rc=$?
check "install refuses to replace a foreign skill"  test "$clash_rc" -ne 0
check "the refusal explains itself"                 grep -q 'refusing to replace' "$WORK/clash.log"
check "their skill is untouched"                    grep -qx THEIRS "$OWN/.claude/skills/code-graph/SKILL.md"
check "their agent is untouched"                    grep -qx THEIRS "$OWN/.claude/agents/terraform.md"
check "nothing was written before the refusal"      test "$(count "$OWN/.claude/skills")" -eq 1
HOME="$OWN" "$REPO/install.sh" --harness claude --scope user --force > "$WORK/force.log" 2>&1
check "--force replaces it"                         test -L "$OWN/.claude/skills/code-graph"
check "rendered agents carry the ownership marker"  grep -q 'installed by ai_agents install.sh' "$OWN/.claude/agents/terraform.md"
# put a foreign directory back where an installed skill was: uninstall must leave it alone
rm -rf "$OWN/.claude/skills/code-graph"; mkdir -p "$OWN/.claude/skills/code-graph"
echo "THEIRS" > "$OWN/.claude/skills/code-graph/SKILL.md"
HOME="$OWN" "$REPO/install.sh" --harness claude --scope user --uninstall > "$WORK/unin.log" 2>&1
check "uninstall keeps a foreign path"              grep -qx THEIRS "$OWN/.claude/skills/code-graph/SKILL.md"
check "uninstall says what it kept"                 grep -q 'kept .*not installed by this tool' "$WORK/unin.log"
check "uninstall removed its own skills"            test "$(count "$OWN/.claude/skills")" -eq 1

check "this checkout was not modified"        test "$(git -C "$REPO" status --porcelain)" = "$BEFORE"

if [ "$fail" -ne 0 ]; then
  echo "install check FAILED; last installer output:"; tail -n 25 "$WORK/project.log" "$WORK/user.log"
  exit 1
fi
echo "install check passed"
