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

echo "== plugin bundles (--plugin) =="
N_SKILLS_PLUGIN=$((N_SKILLS_NO_BOOT + N_AGENTS))   # codex/gemini carry the personas as skills
PH="$WORK/plugin-home"
mkdir -p "$PH/.agents/plugins"
# a marketplace the developer already has: our entry must be added beside theirs and removed alone
printf '{"name": "mine", "plugins": [{"name": "theirs", "source": {"source": "local", "path": "./plugins/theirs"}}]}\n' \
  > "$PH/.agents/plugins/marketplace.json"
HOME="$PH" run_install "$WORK/plugin-user.log" "$REPO/install.sh" --harness all --scope user --plugin
check "claude plugin manifest"                  test -f "$PH/.claude/skills/ai-agents/.claude-plugin/plugin.json"
check "claude plugin agents = $N_AGENTS"        test "$(count "$PH/.claude/skills/ai-agents/agents")" -eq "$N_AGENTS"
check "antigravity plugin manifest"             test -f "$PH/.gemini/config/plugins/ai-agents/plugin.json"
check "antigravity plugin skills = $N_SKILLS_NO_BOOT" test "$(count "$PH/.gemini/config/plugins/ai-agents/skills")" -eq "$N_SKILLS_NO_BOOT"
check "gemini extension skills = $N_SKILLS_PLUGIN"    test "$(count "$PH/.gemini/extensions/ai-agents/skills")" -eq "$N_SKILLS_PLUGIN"
check "copilot plugin agents = $N_AGENTS"       test "$(count "$PH/.copilot/plugins/ai-agents/agents")" -eq "$N_AGENTS"
check "codex plugin skills = $N_SKILLS_PLUGIN"  test "$(count "$PH/plugins/ai-agents/skills")" -eq "$N_SKILLS_PLUGIN"
check "plugin manifests are valid JSON"         python3 -c 'import json,sys; [json.load(open(p)) for p in sys.argv[1:]]' \
  "$PH/.claude/skills/ai-agents/.claude-plugin/plugin.json" "$PH/.gemini/config/plugins/ai-agents/plugin.json" \
  "$PH/.gemini/extensions/ai-agents/gemini-extension.json" "$PH/.copilot/plugins/ai-agents/plugin.json" \
  "$PH/plugins/ai-agents/.codex-plugin/plugin.json"
check "codex marketplace keeps their entry"     grep -q '"theirs"' "$PH/.agents/plugins/marketplace.json"
check "codex marketplace lists ai-agents"       grep -q '"ai-agents"' "$PH/.agents/plugins/marketplace.json"
check "no loose skills next to the plugins"     test ! -e "$PH/.claude/skills/code-graph"
HOME="$PH" run_install "$WORK/plugin-unin.log" "$REPO/install.sh" --harness all --scope user --plugin --uninstall
check "plugin uninstall keeps their marketplace entry" grep -q '"theirs"' "$PH/.agents/plugins/marketplace.json"
check "plugin uninstall removes our entry"      test -z "$(grep '"ai-agents"' "$PH/.agents/plugins/marketplace.json")"
check "plugin uninstall leaves only their file" test "$(cd "$PH" && find . -type f)" = "./.agents/plugins/marketplace.json"

PP="$WORK/plugin-project"
mkdir -p "$PP" && git -C "$PP" init -q
run_install "$WORK/plugin-proj.log" "$REPO/install.sh" --harness all --target "$PP" --plugin
check "project: gemini skipped, not failed"     grep -q 'skip   gemini' "$WORK/plugin-proj.log"
check "project: antigravity plugin"             test -f "$PP/.agents/plugins/ai-agents/plugin.json"
run_install "$WORK/plugin-proj-unin.log" "$REPO/install.sh" --harness all --target "$PP" --plugin --uninstall
check "project: plugin uninstall leaves it clean" test -z "$(git -C "$PP" status --porcelain)"
mkdir -p "$WORK/gem-proj"
# shellcheck disable=SC2016  # single quotes are intended: $0/$1 expand inside bash -c
check "gemini --plugin at project scope is rejected" bash -c '! "$0" --harness gemini --plugin --target "$1" >/dev/null 2>&1' "$REPO/install.sh" "$WORK/gem-proj"
check "the rejected gemini install wrote nothing"    test -z "$(ls -A "$WORK/gem-proj")"

PO="$WORK/plugin-own"
mkdir -p "$PO/.gemini/config/plugins/ai-agents"
echo '{"name": "theirs"}' > "$PO/.gemini/config/plugins/ai-agents/plugin.json"
HOME="$PO" "$REPO/install.sh" --harness antigravity --scope user --plugin > "$WORK/plugin-clash.log" 2>&1 && prc=0 || prc=$?
check "plugin install refuses a foreign folder" test "$prc" -ne 0
check "their plugin is untouched"               grep -q theirs "$PO/.gemini/config/plugins/ai-agents/plugin.json"
HOME="$PO" "$REPO/install.sh" --harness antigravity --scope user --plugin --uninstall > "$WORK/plugin-keep.log" 2>&1
check "plugin uninstall keeps a foreign folder" grep -q theirs "$PO/.gemini/config/plugins/ai-agents/plugin.json"

echo "== --agents-as-skills and a developer's own ~/.agents/skills =="
# The layout seen on a developer Mac: their own skills (a directory and a symlink) in
# ~/.agents/skills, and ~/.gemini/config/skills a symlink to that folder.
DH="$WORK/dev-home"
mkdir -p "$DH/.agents/skills/twg-mine" "$DH/elsewhere/linked-skill" "$DH/.gemini/config"
echo "MINE" > "$DH/.agents/skills/twg-mine/SKILL.md"
echo "LINKED" > "$DH/elsewhere/linked-skill/SKILL.md"
ln -s "$DH/elsewhere/linked-skill" "$DH/.agents/skills/linked-skill"
ln -s "$DH/.agents/skills" "$DH/.gemini/config/skills"
HOME="$DH" run_install "$WORK/as-skills.log" "$REPO/install.sh" --harness antigravity --scope user --agents-as-skills
agent_skills() { for a in "$REPO"/agents/*/; do [ -f "$1/$(basename "$a")/SKILL.md" ] && echo x; done | wc -l | tr -d ' '; }
check "agents installed as skills = $N_AGENTS"      test "$(agent_skills "$DH/.agents/skills")" -eq "$N_AGENTS"
check "native agents still installed = $N_AGENTS"  test "$(count "$DH/.gemini/config/agents")" -eq "$N_AGENTS"
check "skills + agent skills + theirs in the folder" test "$(count "$DH/.agents/skills")" -eq $((N_SKILLS_NO_BOOT + N_AGENTS + 2))
check "their skill directory is untouched"         grep -qx MINE "$DH/.agents/skills/twg-mine/SKILL.md"
check "their symlink is untouched"                 test "$(readlink "$DH/.agents/skills/linked-skill")" = "$DH/elsewhere/linked-skill"
check "HOME/.gemini/config/skills is still their link" test -L "$DH/.gemini/config/skills"
HOME="$DH" run_install "$WORK/as-skills-unin.log" "$REPO/install.sh" --harness antigravity --scope user --uninstall
check "uninstall removes the agent skills"         test ! -e "$DH/.agents/skills/terraform"
check "uninstall leaves only their two skills"     test "$(count "$DH/.agents/skills")" -eq 2
check "their skill survives uninstall"             grep -qx MINE "$DH/.agents/skills/twg-mine/SKILL.md"
check "their symlink survives uninstall"           test -L "$DH/.agents/skills/linked-skill"
check "HOME/.gemini/config/skills link survives"      test -L "$DH/.gemini/config/skills"
# A codex uninstall that empties ~/.agents/skills must not remove it: a link may point at it.
EH="$WORK/empty-home"; mkdir -p "$EH"
HOME="$EH" run_install "$WORK/codex-in.log" "$REPO/install.sh" --harness codex --scope user
HOME="$EH" run_install "$WORK/codex-out.log" "$REPO/install.sh" --harness codex --scope user --uninstall
check "emptied HOME/.agents/skills is kept"           test -d "$EH/.agents/skills"

# Name clashes in ~/.agents/skills: theirs wins, and the run writes nothing at all.
for clash in dir link; do
  CH="$WORK/clash-$clash"; mkdir -p "$CH/.agents/skills" "$CH/.gemini/config" "$CH/theirs/terraform"
  ln -s "$CH/.agents/skills" "$CH/.gemini/config/skills"
  echo "THEIRS" > "$CH/theirs/terraform/SKILL.md"
  if [ "$clash" = dir ]; then cp -R "$CH/theirs/terraform" "$CH/.agents/skills/terraform"
  else ln -s "$CH/theirs/terraform" "$CH/.agents/skills/terraform"; fi
  HOME="$CH" "$REPO/install.sh" --harness antigravity --scope user --agents-as-skills > "$WORK/clash-$clash.log" 2>&1 && crc=0 || crc=$?
  check "clash with their $clash: install refuses"     test "$crc" -ne 0
  check "clash with their $clash: theirs untouched"    grep -qx THEIRS "$CH/.agents/skills/terraform/SKILL.md"
  check "clash with their $clash: no skill written"    test "$(count "$CH/.agents/skills")" -eq 1
  check "clash with their $clash: no agent written"    test ! -e "$CH/.gemini/config/agents"
done
mkdir -p "$WORK/combo"
# shellcheck disable=SC2016  # single quotes are intended: $0/$1 expand inside bash -c
check "--agents-as-skills with --plugin is rejected" bash -c '! HOME="$1" "$0" --harness antigravity --scope user --plugin --agents-as-skills >/dev/null 2>&1' "$REPO/install.sh" "$WORK/combo"
check "the rejected combination wrote nothing"       test -z "$(ls -A "$WORK/combo")"

echo "== IntelliJ's Antigravity agent detected =="
IH="$WORK/ij-home"; mkdir -p "$IH/.gemini/antigravity-acp"
HOME="$IH" run_install "$WORK/ij-loose.log" "$REPO/install.sh" --harness antigravity --scope user
check "regular install suggests --agents-as-skills"  grep -q 'note   IntelliJ.*not custom agents' "$WORK/ij-loose.log"
HOME="$IH" run_install "$WORK/ij-loose-out.log" "$REPO/install.sh" --harness antigravity --scope user --uninstall
HOME="$IH" run_install "$WORK/ij-plugin.log" "$REPO/install.sh" --harness antigravity --scope user --plugin
check "--plugin warns that IntelliJ loads no plugins" grep -q 'warn   IntelliJ.*neither plugins nor custom agents' "$WORK/ij-plugin.log"
check "no IntelliJ: no warning"                      test -z "$(grep 'IntelliJ' "$WORK/plugin-user.log")"

check "this checkout was not modified"        test "$(git -C "$REPO" status --porcelain)" = "$BEFORE"

if [ "$fail" -ne 0 ]; then
  echo "install check FAILED; last installer output:"; tail -n 25 "$WORK/project.log" "$WORK/user.log"
  exit 1
fi
echo "install check passed"
