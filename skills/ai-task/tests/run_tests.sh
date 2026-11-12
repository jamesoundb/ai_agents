#!/usr/bin/env bash
# Tests for ai-task, run against a throwaway workspace with a local bare remote. No network.
# Portable like the script itself (Linux, macOS): CI also runs it under bash 3.2 with BusyBox tools.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="$HERE/../scripts/ai-task"
T="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$T"' EXIT
export HOME="$T/home"; mkdir "$HOME"
git config --global user.name t; git config --global user.email t@example.invalid
D="$(date +%F)"
pass=0 fail=0
# Each check runs in a subshell so that a cd inside one cannot move the next.
ok()  { if ( eval "$2" ) >/dev/null 2>&1; then pass=$((pass + 1)); else fail=$((fail + 1)); echo "FAIL: $1"; fi; }
no()  { if ( eval "$2" ) >/dev/null 2>&1; then fail=$((fail + 1)); echo "FAIL: $1"; else pass=$((pass + 1)); fi; }

git init -q --bare -b main "$T/demo.git"
git clone -q "$T/demo.git" "$T/seed" 2>/dev/null
# The repository carries an old-layout .github/instructions.md, which ai-task must not pick up.
mkdir "$T/seed/.github"; echo 'OLD-LAYOUT-NOTES' > "$T/seed/.github/instructions.md"
git -C "$T/seed" add .github; git -C "$T/seed" commit -q -m first
git -C "$T/seed" push -q origin HEAD:main

W="$T/any-name"; mkdir -p "$W" "$T/outside"
git clone -q "$T/demo.git" "$W/demo"       # a clone that predates the workspace
git -C "$W/demo" remote set-head origin -a >/dev/null

# macOS ships bash 3.2 and BSD sed: none of these GNU-only forms may come back (comments excepted).
no "the script uses no GNU-only commands" "grep -v '^[[:space:]]*#' '$TOOL' | grep -E 'sed -i|readlink -f|mapfile|readarray|\\\\\\+'"
no "every array expansion survives an empty array under bash 3.2" "grep -v '^[[:space:]]*#' '$TOOL' | grep -E '[^+]\"\\\$\\{[A-Za-z_]+\\[@\\]\\}\"'"

no "commands outside a workspace fail" "cd '$T/outside' && '$TOOL' status"
ok "init" "cd '$W' && '$TOOL' init"
ok "init places the workspace copy" "test -x '$W/.ai/bin/ai-task'"
ok "the copy runs on its own" "cd '$W' && .ai/bin/ai-task status"
ok "the git identity reaches the clone (sandboxes replace HOME)" "[ \"\$(git -C '$W/demo' config --local user.email)\" = t@example.invalid ]"
ok "init can be rerun" "cd '$W' && .ai/bin/ai-task init"
mkdir "$W/sub"
no "nested init is refused" "cd '$W/sub' && '$TOOL' init"
rmdir "$W/sub"
no "init inside a checkout is refused" "cd '$W/demo' && '$TOOL' init"
ok "AI_WORKSPACE selects the workspace" "cd '$T/outside' && AI_WORKSPACE='$W' '$TOOL' status"

cd "$W" || exit 1
A=.ai/bin/ai-task
printf '# My problem\n\nDetails.\n' > .ai/instructions/first.md
sum="$(cksum < .ai/instructions/first.md)"
ok "a draft is listed as not started" "$A status | grep -q 'not started: first'"
ok "start with repo" "$A start first demo"
L=".ai/ai_work_logs/$D-first.md"
ok "the problem file is left unchanged" "[ '$sum' = \"\$(cksum < .ai/instructions/first.md)\" ]"
ok "the title comes from the problem file" "grep -q '^title: My problem' $L"
ok "the worktree is on the feature branch" "[ \"\$(git -C .tasks/$D-first/demo branch --show-current)\" = feature/first ]"
no "the new branch has no upstream" "git -C .tasks/$D-first/demo rev-parse --abbrev-ref '@{u}'"
ok "the clone stays on main" "[ \"\$(git -C demo branch --show-current)\" = main ]"
ok "the reference file exists" "test -f .ai/instructions/reference/demo.md"
no "a repo's .github/instructions.md is not copied" "grep -q OLD-LAYOUT-NOTES .ai/instructions/reference/demo.md"
ok "the reference starts as the plain stub" "grep -q '^Living reference' .ai/instructions/reference/demo.md"
ok "discovery works inside a worktree" "cd .tasks/$D-first/demo && '$TOOL' status"
# BSD wc pads its counts ("       0"), which broke this line and the unpushed check in close.
# The branch column is 30 wide plus one space: exactly 18 spaces after feature/first.
ok "status counts are plain numbers" "$A status | grep -qE 'feature/first {18}0 changed, 0 unpushed'"

no "an empty worklog blocks close" "$A close first"
ok "note adds an entry" "$A note first 'found the cause'"
ok "note stamps the real date" "grep -q \"^## $D \" $L"
no "note on an unknown task fails" "$A note nope text"

echo x > ".tasks/$D-first/demo/f"
no "uncommitted work blocks close" "$A close first"
ok "a refused close does not archive" "test -f .ai/instructions/first.md"
git -C ".tasks/$D-first/demo" add f; git -C ".tasks/$D-first/demo" commit -q -m work
no "unpushed work blocks close" "$A close first"
git -C ".tasks/$D-first/demo" push -q -u origin feature/first
chmod 640 "$L"
ok "pushed work closes, from inside the task" "cd .tasks/$D-first/demo && '$TOOL' close first"
ok "rewriting the log's fields keeps its permissions" "ls -l $L | grep -q '^-rw-r-----'"
ok "the problem file is archived" "test -f .ai/instructions/archive/$D-first.md"
ok "the log points at the archive" "grep -q '^instructions: .ai/instructions/archive/' $L"
ok "the log is marked done" "grep -q '^status: done' $L"
no "the local branch is gone once pushed" "git -C demo rev-parse --verify -q feature/first"
ok "the index shows it" "grep -q 'done.*$D-first' .ai/ai_work_logs/INDEX.md"
printf '# again\n' > .ai/instructions/first.md
no "a closed slug is not reused the same day" "$A start first"
rm .ai/instructions/first.md

ok "start with no repos" "$A start solo --title 'No repos'"
ok "no branch is recorded" "grep -q '^branch: *$' .ai/ai_work_logs/$D-solo.md"
ok "a stub problem file is created" "test -s .ai/instructions/solo.md"
ok "a repo can be added" "$A start solo demo"
ok "the branch is recorded once a repo is added" "grep -q '^branch: feature/solo' .ai/ai_work_logs/$D-solo.md"
echo x > ".tasks/$D-solo/demo/g"
echo x > ".tasks/$D-solo/stray.txt"        # not created by ai-task: kept, and named in the output
ok "--force closes and names a stray file" "$A close solo --force 2>&1 | grep -q 'left in place.*stray.txt'"
ok "the stray file is kept" "test -f .tasks/$D-solo/stray.txt"
ok "list finds tasks by repo" "$A list demo | grep -q $D-solo"
# Same date, so name order decides: solo sorts after first and must be listed above it.
ok "the index lists newest first" "[ \"\$(grep -oE '\\[$D-(first|solo)\\]' .ai/ai_work_logs/INDEX.md | tr -d '\\n')\" = '[$D-solo][$D-first]' ]"
ok "the tracking repository is clean" "[ -z \"\$(git -C .ai status --short)\" ]"
ok "sync with no names visits every clone" "$A sync | grep -q '^ai-task: demo:'"

# install.sh links the script into ~/.local/bin; init through that link must copy the script itself.
mkdir -p "$T/bin" "$T/linked"
ln -s "$TOOL" "$T/bin/ai-task"
ok "init through a PATH link" "cd '$T/linked' && PATH='$T/bin:$PATH' ai-task init"
ok "the workspace copy is the script, not a link" "test -f '$T/linked/.ai/bin/ai-task' && test ! -L '$T/linked/.ai/bin/ai-task' && cmp -s '$TOOL' '$T/linked/.ai/bin/ai-task'"

echo "$pass passed, $fail failed"
[ "$fail" = 0 ]
