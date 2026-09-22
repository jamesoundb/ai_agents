#!/usr/bin/env bash
# install.sh — install the agents and skills from this repo into an AI coding harness.
#
#   ./install.sh --harness antigravity --scope user  # global install (~/.gemini/config/{agents,skills})
#   ./install.sh --harness antigravity               # project scope, current directory
#   ./install.sh --harness claude,codex --target ~/work/app
#   ./install.sh --harness antigravity --copy        # copy instead of symlink (Windows, CI images)
#   ./install.sh --harness antigravity --uninstall
#
# Options:
#   --harness LIST   claude, codex, gemini, antigravity, copilot (comma list) or all   (required)
#   --scope S        project (default: files in --target) or user (your home directory)
#   --target DIR     project to install into (default: current directory; must already exist)
#   --copy           copy files instead of symlinking them into this clone
#   --agents LIST    install only these agents (comma list)
#   --skills LIST    install only these skills (comma list)
#   --uninstall      remove what an install with the same options added
#   --force          overwrite (or remove) paths this installer did not create
#   -h, --help       show this help
#
# Harness layouts (project scope | user scope):
#   claude      .claude/skills/<s>, .claude/agents/<a>.md            | ~/.claude/skills, ~/.claude/agents
#   codex       .agents/skills/<s>, .agents/skills/<a>/SKILL.md      | ~/.agents/skills
#   antigravity .agents/skills/<s>, .agents/agents/<a>/agent.md      | ~/.gemini/config/skills, ~/.gemini/config/agents
#   gemini      .gemini/skills/<s>, .gemini/skills/<a>/SKILL.md, GEMINI.md (@AGENTS.md) | ~/.gemini/skills
#   copilot     .github/skills/<s>, .github/agents/<a>.agent.md      | ~/.copilot/skills, ~/.copilot/agents
# Project scope also maintains a managed block in AGENTS.md (read by Codex, Gemini, Copilot, Cursor)
# and, for claude, a CLAUDE.md that imports AGENTS.md.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_SRC="$REPO/skills"
AGENTS_SRC="$REPO/agents"
RENDER="$REPO/tools/render.py"

HARNESSES=""; SCOPE="project"; TARGET="$PWD"; MODE="link"; UNINSTALL=0; ONLY_AGENTS=""; ONLY_SKILLS=""; FORCE=0

# Print the comment header (line 2 up to the first line that is not a comment) as the help text.
usage() { awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --harness) HARNESSES="$2"; shift 2 ;;
    --scope) SCOPE="$2"; shift 2 ;;
    --target) [ -d "$2" ] || { echo "--target: directory not found: $2 (create it first)" >&2; exit 1; }
              TARGET="$(cd "$2" && pwd)"; shift 2 ;;
    --copy) MODE="copy"; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --force) FORCE=1; shift ;;
    --agents) ONLY_AGENTS="$2"; shift 2 ;;
    --skills) ONLY_SKILLS="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown option: $1" >&2; usage 1 ;;
  esac
done
[ -n "$HARNESSES" ] || { echo "--harness is required (claude, codex, gemini, antigravity, copilot, all)" >&2; exit 1; }
case "$SCOPE" in
  project|user) ;;
  *) echo "--scope must be project or user (got: $SCOPE); for a global install use --scope user" >&2; exit 1 ;;
esac
[ "$HARNESSES" = "all" ] && HARNESSES="claude,codex,gemini,antigravity,copilot"
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

# Which agents/skills to install: explicit lists or everything in the repo.
if [ -n "$ONLY_AGENTS" ]; then AGENTS="${ONLY_AGENTS//,/ }"; else AGENTS="$(ls "$AGENTS_SRC" | tr "\n" " ")"; fi
# install-agents is the bootstrap skill of this repo; it is only installed when named explicitly.
if [ -n "$ONLY_SKILLS" ]; then SKILLS="${ONLY_SKILLS//,/ }"; else SKILLS="$(ls "$SKILLS_SRC" | grep -v '^install-agents$' | tr "\n" " ")"; fi
# Agents declare the skills they need; make sure those are included.
for a in $AGENTS; do
  for s in $(sed -n 's/^skills: *\[\(.*\)\]/\1/p' "$AGENTS_SRC/$a/AGENT.md" | tr ',' ' '); do
    case " $SKILLS " in *" $s "*) ;; *) SKILLS="$SKILLS $s" ;; esac
  done
done

log() { printf '  %s\n' "$*"; }

# ---------------------------------------------------------------------------------------------
# Ownership: this installer only replaces or removes paths it created itself, so a developer's own
# skills and agents are never destroyed. Ownership is proven from the files themselves, with no
# state kept anywhere: a skill is ours when it is a symlink into this repo or a copy carrying our
# marker file; a rendered agent is ours when it carries the marker line (or, for installs made
# before markers existed, when its content is exactly what we render now). --force overrides.
MARKER="<!-- installed by ai_agents install.sh; edit the repo and re-run the installer instead -->"
MARKER_FILE=".installed-by-ai-agents"

marked_dir() {  # a copied skill directory we wrote
  [ -d "$1" ] && [ -f "$1/$MARKER_FILE" ]
}
marked_file() {  # a rendered file we wrote
  [ -f "$1" ] && grep -qxF "$MARKER" "$1" 2>/dev/null
}
symlink_into_repo() {
  [ -L "$1" ] || return 1
  case "$(readlink -f "$1" 2>/dev/null)" in "$REPO"/*) return 0 ;; *) return 1 ;; esac
}
ours_skill() {  # <path> to an installed skill (symlink, or copied directory)
  symlink_into_repo "$1" || marked_dir "$1"
}
ours_agent() {  # <renderer> <agent> <file> [prefix]
  marked_file "$3" && return 0
  [ -f "$3" ] || return 1
  { python3 "$RENDER" "$1" "$AGENTS_SRC/$2/AGENT.md" ${4:+"$4"} 2>/dev/null; } | cmp -s - "$3"
}
refuse() {
  echo "refusing to replace $1" >&2
  echo "  it was not installed by this tool, so overwriting it could destroy your own work." >&2
  echo "  move it aside, or re-run with --force to replace it." >&2
  exit 1
}
guard_skill() {  # stop before replacing a skill path we did not create
  { [ -e "$1" ] || [ -L "$1" ]; } || return 0
  ours_skill "$1" && return 0
  [ "$FORCE" = 1 ] && { log "force  replacing $1 (not installed by this tool)"; return 0; }
  refuse "$1"
}
guard_agent() {  # <renderer> <agent> <file> [prefix]
  { [ -e "$3" ] || [ -L "$3" ]; } || return 0
  ours_agent "$1" "$2" "$3" ${4:+"$4"} && return 0
  [ "$FORCE" = 1 ] && { log "force  replacing $3 (not installed by this tool)"; return 0; }
  refuse "$3"
}
DRY=0   # 1 = check destinations only, write nothing (pre-flight pass)

place_skill() {  # place_skill <skill> <dest_dir>
  local s="$1" dest="$2/$1"
  mkdir -p "$2"
  guard_skill "$dest"
  [ "$DRY" = 1 ] && return 0
  rm -rf "$dest"
  if [ "$MODE" = "copy" ]; then
    cp -R "$SKILLS_SRC/$s" "$dest"
    printf '%s\n' "$REPO" > "$dest/$MARKER_FILE"   # proves we wrote this copy
  else
    ln -s "$SKILLS_SRC/$s" "$dest"
  fi
  log "skill  $dest  <- $( [ "$MODE" = copy ] && echo copy || echo symlink )"
}

remove_path() {  # remove an installed skill; anything we did not create is left alone
  [ -e "$1" ] || [ -L "$1" ] || return 0
  if ours_skill "$1" || [ "$FORCE" = 1 ]; then rm -rf "$1"; log "removed $1"
  else log "kept   $1 (not installed by this tool)"; fi
}

remove_agent() {  # remove_agent <renderer> <agent> <path> [prefix]; <path> is the rendered file or
  # the directory holding it (Antigravity). Only a file we wrote is removed.
  local file="$3"
  case "$3" in *.md) ;; *) file="$3/agent.md" ;; esac
  if ours_agent "$1" "$2" "$file" ${4:+"$4"} || [ "$FORCE" = 1 ]; then rm -rf "$3"; log "removed $3"
  else log "kept   $3 (not installed by this tool)"; fi
}

write_rendered() {  # write_rendered <renderer> <agent> <out_file> [extra renderer arg]
  mkdir -p "$(dirname "$3")"
  guard_agent "$1" "$2" "$3" ${4:+"$4"}
  [ "$DRY" = 1 ] && return 0
  { python3 "$RENDER" "$1" "$AGENTS_SRC/$2/AGENT.md" ${4:+"$4"}; printf '%s\n' "$MARKER"; } > "$3"
  log "agent  $3  <- rendered ($1)"
}

update_agents_md() {  # maintain the managed block in <target>/AGENTS.md
  local f="$TARGET/AGENTS.md" block tmp
  block="$(python3 "$RENDER" agents-md "$AGENTS_SRC" "$SKILLS_SRC")"
  tmp="$(mktemp)"
  if [ -f "$f" ] && grep -q 'BEGIN managed by install.sh' "$f"; then
    awk -v block="$block" '
      /BEGIN managed by install.sh/ {print block; skip=1; next}
      /END managed by install.sh/ {skip=0; next}
      !skip {print}' "$f" > "$tmp"
  elif [ -f "$f" ]; then
    { cat "$f"; printf '\n'; printf '%s\n' "$block"; } > "$tmp"
  else
    { printf '# AGENTS.md\n\nInstructions for AI coding agents working in this repository.\n\n'; printf '%s\n' "$block"; } > "$tmp"
  fi
  mv "$tmp" "$f"; log "wrote  $f (managed block)"
}

remove_agents_md_block() {
  local f="$TARGET/AGENTS.md" tmp
  [ -f "$f" ] && grep -q 'BEGIN managed by install.sh' "$f" || return 0
  tmp="$(mktemp)"
  # drop the block and the blank line the install put in front of it; keep everything else byte-for-byte
  awk '
    /BEGIN managed by install.sh/ {skip=1; pending=0; next}
    /END managed by install.sh/ {skip=0; next}
    skip {next}
    /^$/ {pending++; next}
    {while (pending > 0) {print ""; pending--}; print}
    END {while (pending > 0) {print ""; pending--}}' "$f" > "$tmp"
  # an AGENTS.md that only ever held our boilerplate header goes away entirely
  if [ "$(tr -d '\n' < "$tmp")" = "# AGENTS.mdInstructions for AI coding agents working in this repository." ] || [ ! -s "$tmp" ]; then
    rm -f "$tmp" "$f"; log "removed $f (nothing left but the installer's header)"
  else
    mv "$tmp" "$f"; log "removed managed block from $f"
  fi
}

ensure_import() {  # ensure_import <file> <line>  (create file or append the import line once)
  if [ ! -f "$1" ]; then printf '%s\n' "$2" > "$1"; log "wrote  $1"
  elif ! grep -qF "$2" "$1"; then printf '\n%s\n' "$2" >> "$1"; log "added '$2' to $1"; fi
}

remove_import() {  # remove_import <file> <line>  (undo ensure_import: delete the file if that is all it holds)
  [ -f "$1" ] || return 0
  if [ "$(tr -d '\n' < "$1")" = "$2" ]; then rm -f "$1"; log "removed $1"
  elif grep -qxF "$2" "$1"; then
    local tmp; tmp="$(mktemp)"
    awk -v line="$2" '$0 == line {if (prev_blank) {blank_dropped=1}; next} {if (prev_blank && !blank_dropped) print ""; blank_dropped=0; prev_blank=($0 == ""); if (!prev_blank) print} END {}' "$1" > "$tmp"
    mv "$tmp" "$1"; log "removed '$2' from $1"
  fi
}

remove_empty_dirs() {  # remove_empty_dirs <dir>...  (leave no empty harness folders behind)
  for d in "$@"; do [ -d "$d" ] && [ -z "$(ls -A "$d" 2>/dev/null)" ] && rmdir "$d" 2>/dev/null && log "removed empty $d"; done; return 0
}

run_harnesses() {
  for h in ${HARNESSES//,/ }; do
    echo "[$h] scope=$SCOPE target=$TARGET"
    case "$h" in
      claude)
        if [ "$SCOPE" = user ]; then SK="$HOME/.claude/skills"; AG="$HOME/.claude/agents"; else SK="$TARGET/.claude/skills"; AG="$TARGET/.claude/agents"; fi
        if [ $UNINSTALL = 1 ]; then
          for s in $SKILLS; do remove_path "$SK/$s"; done; for a in $AGENTS; do remove_agent claude "$a" "$AG/$a.md"; done
          [ "$SCOPE" = project ] && remove_import "$TARGET/CLAUDE.md" "@AGENTS.md"
          remove_empty_dirs "$SK" "$AG" "$(dirname "$SK")"
        else
          for s in $SKILLS; do place_skill "$s" "$SK"; done
          for a in $AGENTS; do write_rendered claude "$a" "$AG/$a.md"; done
          [ "$SCOPE" = project ] && ensure_import "$TARGET/CLAUDE.md" "@AGENTS.md"
        fi ;;
      codex)
        if [ "$SCOPE" = user ]; then SK="$HOME/.agents/skills"; else SK="$TARGET/.agents/skills"; fi
        if [ $UNINSTALL = 1 ]; then
          for s in $SKILLS; do remove_path "$SK/$s"; done; for a in $AGENTS; do remove_agent skill "$a" "$SK/$a/SKILL.md"; rmdir "$SK/$a" 2>/dev/null || true; done
          remove_empty_dirs ${AG:+"$AG"} "$SK" "$(dirname "$SK")"
        else
          for s in $SKILLS; do place_skill "$s" "$SK"; done
          for a in $AGENTS; do write_rendered skill "$a" "$SK/$a/SKILL.md"; done
        fi ;;
      antigravity)
        # Native custom agents: .agents/agents/<a>/agent.md; `skills:` entries are paths to the
        # installed skill folders (workspace-relative for project scope, absolute for user scope).
        if [ "$SCOPE" = user ]; then SK="$HOME/.gemini/config/skills"; AG="$HOME/.gemini/config/agents"; PREFIX="$SK"
        else SK="$TARGET/.agents/skills"; AG="$TARGET/.agents/agents"; PREFIX=".agents/skills"; fi
        if [ $UNINSTALL = 1 ]; then
          for s in $SKILLS; do remove_path "$SK/$s"; done; for a in $AGENTS; do remove_agent antigravity "$a" "$AG/$a" "$PREFIX"; done
          remove_empty_dirs "$AG" "$SK" "$(dirname "$SK")"
        else
          for s in $SKILLS; do place_skill "$s" "$SK"; done
          for a in $AGENTS; do write_rendered antigravity "$a" "$AG/$a/agent.md" "$PREFIX"; done
        fi ;;
      gemini)
        if [ "$SCOPE" = user ]; then SK="$HOME/.gemini/skills"; else SK="$TARGET/.gemini/skills"; fi
        if [ $UNINSTALL = 1 ]; then
          for s in $SKILLS; do remove_path "$SK/$s"; done; for a in $AGENTS; do remove_agent skill "$a" "$SK/$a/SKILL.md"; rmdir "$SK/$a" 2>/dev/null || true; done
          [ "$SCOPE" = project ] && remove_import "$TARGET/GEMINI.md" "@AGENTS.md"
          remove_empty_dirs "$SK" "$(dirname "$SK")"
        else
          for s in $SKILLS; do place_skill "$s" "$SK"; done
          for a in $AGENTS; do write_rendered skill "$a" "$SK/$a/SKILL.md"; done
          [ "$SCOPE" = project ] && ensure_import "$TARGET/GEMINI.md" "@AGENTS.md"
        fi ;;
      copilot)
        if [ "$SCOPE" = user ]; then SK="$HOME/.copilot/skills"; AG="$HOME/.copilot/agents"; else SK="$TARGET/.github/skills"; AG="$TARGET/.github/agents"; fi
        if [ $UNINSTALL = 1 ]; then
          for s in $SKILLS; do remove_path "$SK/$s"; done; for a in $AGENTS; do remove_agent copilot "$a" "$AG/$a.agent.md"; done
          remove_empty_dirs "$AG" "$SK" "$(dirname "$SK")"
        else
          for s in $SKILLS; do place_skill "$s" "$SK"; done
          for a in $AGENTS; do write_rendered copilot "$a" "$AG/$a.agent.md"; done
        fi ;;
      *) echo "unknown harness: $h" >&2; exit 1 ;;
    esac
  done
  return 0   # the last branch may end on a false test (e.g. [ "$SCOPE" = project ]); that is not a failure
}

# Pre-flight: check every destination before writing anything, so a clash cannot leave a
# half-finished install behind.
if [ $UNINSTALL = 0 ]; then DRY=1; run_harnesses > /dev/null; DRY=0; fi
run_harnesses

if [ "$SCOPE" = project ]; then
  if [ $UNINSTALL = 1 ]; then remove_agents_md_block; else update_agents_md; fi
fi
[ $UNINSTALL = 1 ] && echo "done (uninstall)" || echo "done. Add '.ast-graph/' to the target's .gitignore; the graph is a build artifact."
