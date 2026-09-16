#!/usr/bin/env bash
# install.sh — install the agents and skills from this repo into an AI coding harness.
#
#   ./install.sh --harness all                       # project scope, current directory
#   ./install.sh --harness claude,codex --target ~/work/app
#   ./install.sh --harness gemini --scope user       # into ~/.gemini/skills
#   ./install.sh --harness all --copy                # copy instead of symlink (Windows, CI images)
#   ./install.sh --harness all --uninstall
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

HARNESSES=""; SCOPE="project"; TARGET="$PWD"; MODE="link"; UNINSTALL=0; ONLY_AGENTS=""; ONLY_SKILLS=""

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --harness) HARNESSES="$2"; shift 2 ;;
    --scope) SCOPE="$2"; shift 2 ;;
    --target) TARGET="$(cd "$2" && pwd)"; shift 2 ;;
    --copy) MODE="copy"; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --agents) ONLY_AGENTS="$2"; shift 2 ;;
    --skills) ONLY_SKILLS="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown option: $1" >&2; usage 1 ;;
  esac
done
[ -n "$HARNESSES" ] || { echo "--harness is required (claude, codex, gemini, antigravity, copilot, all)" >&2; exit 1; }
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

place_skill() {  # place_skill <skill> <dest_dir>
  local s="$1" dest="$2/$1"
  mkdir -p "$2"
  rm -rf "$dest"
  if [ "$MODE" = "copy" ]; then cp -R "$SKILLS_SRC/$s" "$dest"; else ln -s "$SKILLS_SRC/$s" "$dest"; fi
  log "skill  $dest  <- $( [ "$MODE" = copy ] && echo copy || echo symlink )"
}

remove_path() { [ -e "$1" ] || [ -L "$1" ] || return 0; rm -rf "$1"; log "removed $1"; }

write_rendered() {  # write_rendered <renderer> <agent> <out_file> [extra renderer arg]
  mkdir -p "$(dirname "$3")"
  python3 "$RENDER" "$1" "$AGENTS_SRC/$2/AGENT.md" ${4:+"$4"} > "$3"
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

for h in ${HARNESSES//,/ }; do
  echo "[$h] scope=$SCOPE target=$TARGET"
  case "$h" in
    claude)
      if [ "$SCOPE" = user ]; then SK="$HOME/.claude/skills"; AG="$HOME/.claude/agents"; else SK="$TARGET/.claude/skills"; AG="$TARGET/.claude/agents"; fi
      if [ $UNINSTALL = 1 ]; then
        for s in $SKILLS; do remove_path "$SK/$s"; done; for a in $AGENTS; do remove_path "$AG/$a.md"; done
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
        for s in $SKILLS; do remove_path "$SK/$s"; done; for a in $AGENTS; do remove_path "$SK/$a"; done
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
        for s in $SKILLS; do remove_path "$SK/$s"; done; for a in $AGENTS; do remove_path "$AG/$a"; done
        remove_empty_dirs "$AG" "$SK" "$(dirname "$SK")"
      else
        for s in $SKILLS; do place_skill "$s" "$SK"; done
        for a in $AGENTS; do write_rendered antigravity "$a" "$AG/$a/agent.md" "$PREFIX"; done
      fi ;;
    gemini)
      if [ "$SCOPE" = user ]; then SK="$HOME/.gemini/skills"; else SK="$TARGET/.gemini/skills"; fi
      if [ $UNINSTALL = 1 ]; then
        for s in $SKILLS; do remove_path "$SK/$s"; done; for a in $AGENTS; do remove_path "$SK/$a"; done
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
        for s in $SKILLS; do remove_path "$SK/$s"; done; for a in $AGENTS; do remove_path "$AG/$a.agent.md"; done
        remove_empty_dirs "$AG" "$SK" "$(dirname "$SK")"
      else
        for s in $SKILLS; do place_skill "$s" "$SK"; done
        for a in $AGENTS; do write_rendered copilot "$a" "$AG/$a.agent.md"; done
      fi ;;
    *) echo "unknown harness: $h" >&2; exit 1 ;;
  esac
done

if [ "$SCOPE" = project ]; then
  if [ $UNINSTALL = 1 ]; then remove_agents_md_block; else update_agents_md; fi
fi
[ $UNINSTALL = 1 ] && echo "done (uninstall)" || echo "done. Add '.ast-graph/' to the target's .gitignore; the graph is a build artifact."
