#!/usr/bin/env bash
# install.sh — install the agents and skills from this repo into an AI coding harness.
#
#   ./install.sh --harness antigravity --scope user  # global install (~/.gemini/config/{agents,skills})
#   ./install.sh --harness antigravity               # project scope, current directory
#   ./install.sh --harness claude,codex --target ~/work/app
#   ./install.sh --harness antigravity --copy        # copy instead of symlink (Windows, CI images)
#   ./install.sh --harness antigravity --uninstall
#   ./install.sh --harness antigravity,claude --scope user --plugin   # as one plugin per harness
#   git pull && ./install.sh --update                 # refresh every install this clone made
#
# Options:
#   --harness LIST   claude, codex, gemini, antigravity, copilot (comma list) or all
#                    (required, except with --update)
#   --scope S        project (default: files in --target) or user (your home directory)
#   --target DIR     project to install into (default: current directory; must already exist)
#   --plugin         install one plugin bundle per harness (layouts below) instead of loose
#                    skills and agents; IDE front ends load plugins the same way as the CLIs
#   --agents-as-skills
#                    antigravity: also install each agent as a skill (<skills>/<agent>/SKILL.md),
#                    for front ends that load skills but not custom agents (IntelliJ's
#                    Antigravity agent). codex and gemini always install agents this way.
#   --copy           copy files instead of symlinking them into this clone
#   --agents LIST    install only these agents (comma list)
#   --skills LIST    install only these skills (comma list)
#   --update         refresh what this clone already installed, keeping each install's options
#                    (harness, plugin or loose, copy or symlink, agent/skill lists,
#                    --agents-as-skills). User scope by default; --target DIR (or --scope
#                    project) updates that project instead; --harness narrows it.
#   --uninstall      remove what an install with the same options added
#   --force          overwrite (or remove) paths this installer did not create
#   -h, --help       show this help
#
# Harness layouts (project scope | user scope):
#   claude      .claude/skills/<s>, .claude/agents/<a>.md            | ~/.claude/skills, ~/.claude/agents
#   codex       .agents/skills/<s>, .agents/skills/<a>/SKILL.md      | ~/.agents/skills
#   antigravity .agents/skills/<s>, .agents/agents/<a>/agent.md      | ~/.gemini/config/skills, ~/.gemini/config/agents
#   gemini      .gemini/skills/<s>, .gemini/skills/<a>/SKILL.md, GEMINI.md (@AGENTS.md) | ~/.agents/skills
#               (shared with Codex; Gemini CLI reads it besides ~/.gemini/skills, and ~/.agents wins)
#   copilot     .github/skills/<s>, .github/agents/<a>.agent.md      | ~/.copilot/skills, ~/.copilot/agents
# claude with code-graph also gets a SessionStart hook that builds the code graph in the background
# (plugin: hooks/hooks.json; loose: .claude/settings.local.json | ~/.claude/settings.json, our entry
# only). Turn it off per environment with ASTGRAPH_AUTOBUILD=0; --uninstall removes it.
# Plugin layouts with --plugin (project scope | user scope), each holding skills/ and agents:
#   claude      .claude/skills/ai-agents/.claude-plugin/plugin.json  | ~/.claude/skills/ai-agents (loads as ai-agents@skills-dir)
#   antigravity .agents/plugins/ai-agents/plugin.json                | ~/.gemini/config/plugins/ai-agents
#   gemini      (user scope only)                                    | ~/.gemini/extensions/ai-agents/gemini-extension.json
#   copilot     .github/plugins/ai-agents/plugin.json                | ~/.copilot/plugins/ai-agents (VS Code: add the
#               printed path to the chat.pluginLocations setting)
#   codex       plugins/ai-agents/.codex-plugin/plugin.json + .agents/plugins/marketplace.json
#               | ~/plugins/ai-agents + ~/.agents/plugins/marketplace.json (then install it from /plugins)
# Project scope also maintains a managed block in AGENTS.md (read by Codex, Gemini, Copilot, Cursor)
# and, for claude, a CLAUDE.md that imports AGENTS.md.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_SRC="$REPO/skills"
AGENTS_SRC="$REPO/agents"
RENDER="$REPO/tools/render.py"

HARNESSES=""; SCOPE="project"; TARGET="$PWD"; MODE="link"; UNINSTALL=0; ONLY_AGENTS=""; ONLY_SKILLS=""; FORCE=0; PLUGIN=0; ALL=0; AGENT_SKILLS=0; UPDATE=0; SCOPE_SET=0; TARGET_SET=0

# Print the comment header (line 2 up to the first line that is not a comment) as the help text.
usage() { awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --harness) HARNESSES="$2"; shift 2 ;;
    --scope) SCOPE="$2"; SCOPE_SET=1; shift 2 ;;
    --target) [ -d "$2" ] || { echo "--target: directory not found: $2 (create it first)" >&2; exit 1; }
              TARGET="$(cd "$2" && pwd)"; TARGET_SET=1; shift 2 ;;
    --update) UPDATE=1; shift ;;
    --copy) MODE="copy"; shift ;;
    --plugin) PLUGIN=1; shift ;;
    --agents-as-skills) AGENT_SKILLS=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --force) FORCE=1; shift ;;
    --agents) ONLY_AGENTS="$2"; shift 2 ;;
    --skills) ONLY_SKILLS="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown option: $1" >&2; usage 1 ;;
  esac
done
if [ "$UPDATE" = 1 ]; then
  # Everything else is read back from what is installed; an option here would contradict it.
  if [ "$UNINSTALL" = 1 ] || [ "$PLUGIN" = 1 ] || [ "$MODE" = copy ] || [ "$AGENT_SKILLS" = 1 ] \
     || [ -n "$ONLY_AGENTS" ] || [ -n "$ONLY_SKILLS" ]; then
    echo "--update takes only --harness, --scope, --target and --force; the rest is read from what is installed" >&2; exit 1
  fi
  [ -n "$HARNESSES" ] || HARNESSES="all"
  # a global update unless a project was named
  [ "$SCOPE_SET" = 1 ] || { [ "$TARGET_SET" = 1 ] && SCOPE=project || SCOPE=user; }
fi
[ -n "$HARNESSES" ] || { echo "--harness is required (claude, codex, gemini, antigravity, copilot, all)" >&2; exit 1; }
case "$SCOPE" in
  project|user) ;;
  *) echo "--scope must be project or user (got: $SCOPE); for a global install use --scope user" >&2; exit 1 ;;
esac
[ "$HARNESSES" = "all" ] && { HARNESSES="claude,codex,gemini,antigravity,copilot"; ALL=1; }
# IntelliJ, the reason for --agents-as-skills, loads no plugins: the combination would do nothing.
if [ "$AGENT_SKILLS" = 1 ] && [ "$PLUGIN" = 1 ]; then
  echo "--agents-as-skills applies to the regular install; IntelliJ's Antigravity agent does not load plugins" >&2; exit 1
fi
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

# Which agents/skills to install: explicit lists or everything in the repo.
if [ -n "$ONLY_AGENTS" ]; then AGENTS="${ONLY_AGENTS//,/ }"; else AGENTS="$(for d in "$AGENTS_SRC"/*/; do basename "$d"; done | tr "\n" " ")"; fi
# install-agents is the bootstrap skill of this repo; it is only installed when named explicitly.
if [ -n "$ONLY_SKILLS" ]; then SKILLS="${ONLY_SKILLS//,/ }"; else SKILLS="$(for d in "$SKILLS_SRC"/*/; do n="$(basename "$d")"; [ "$n" = install-agents ] || printf '%s ' "$n"; done)"; fi
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
resolve_link() {  # portable `readlink -f`: macOS readlink has no -f. Plain readlink only.
  local target="$1" dir
  while [ -L "$target" ]; do
    dir="$(dirname "$target")"
    target="$(readlink "$target")" || return 1
    case "$target" in /*) ;; *) target="$dir/$target" ;; esac
  done
  printf '%s\n' "$target"
}
symlink_into_repo() {
  [ -L "$1" ] || return 1
  # Without this being portable, macOS silently fails the ownership test and the installer
  # treats its own symlinks as foreign: it then refuses to replace them without --force.
  case "$(resolve_link "$1" 2>/dev/null)" in "$REPO"/*) return 0 ;; *) return 1 ;; esac
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
  echo "  it does not prove it came from this installer, so overwriting it could destroy your work." >&2
  echo "  two cases look identical here:" >&2
  echo "    - you edited it, or it is your own file: move it aside." >&2
  echo "    - it is an older install from before the ownership marker existed, and the agent" >&2
  echo "      definition has since changed upstream. Diff it against the canonical file under" >&2
  echo "      agents/<name>/AGENT.md; if the only differences are the upstream ones, re-run with" >&2
  echo "      --force. Installs from this version onward carry the marker and upgrade cleanly." >&2
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
  # --update --force: only the exact unmarked agent files detection named (TAKEOVER_FILES)
  { [ "$FORCE" = 1 ] || case " ${TAKEOVER_FILES:-} " in *" $3 "*) true ;; *) false ;; esac; } && { log "force  replacing $3 (not installed by this tool)"; return 0; }
  refuse "$3"
}
DRY=0   # 1 = check destinations only, write nothing (pre-flight pass)

place_skill() {  # place_skill <skill> <dest_dir>
  local s="$1" dest="$2/$1"
  guard_skill "$dest"
  [ "$DRY" = 1 ] && return 0   # pre-flight: create nothing, so a refusal leaves no trace
  mkdir -p "$2"
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
  guard_agent "$1" "$2" "$3" ${4:+"$4"}
  [ "$DRY" = 1 ] && return 0   # pre-flight: create nothing, so a refusal leaves no trace
  mkdir -p "$(dirname "$3")"
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
  local d
  for d in "$@"; do
    # ~/.agents/skills is the cross-tool skills folder (Codex, Gemini CLI, VS Code; often the
    # target of a developer's own symlinks, e.g. ~/.gemini/config/skills -> ~/.agents/skills).
    # Removing it, even empty, would leave those links dangling, so it and ~/.agents stay.
    case "$d" in "$HOME/.agents"|"$HOME/.agents/skills") continue ;; esac
    [ -d "$d" ] && [ ! -L "$d" ] && [ -z "$(ls -A "$d" 2>/dev/null)" ] && rmdir "$d" 2>/dev/null && log "removed empty $d"
  done; return 0
}

intellij_antigravity() {  # print where IntelliJ's Antigravity ACP agent was found; return 1 if absent
  # JetBrains caches ACP agents per IDE (macOS: ~/Library/Caches/JetBrains/<IDE>/acp-agents/,
  # Linux: ~/.cache/JetBrains/<IDE>/acp-agents/); the agent keeps its state in
  # ~/.gemini/antigravity-acp. Unmatched globs stay literal and fail the -d test (bash 3.2 safe).
  local d
  for d in "$HOME"/Library/Caches/JetBrains/*/acp-agents/antigravity-acp \
           "${XDG_CACHE_HOME:-$HOME/.cache}"/JetBrains/*/acp-agents/antigravity-acp \
           "$HOME/.gemini/antigravity-acp"; do
    [ -d "$d" ] && { printf '%s\n' "$d"; return 0; }
  done
  return 1
}

# ---------------------------------------------------------------------------------------------
# Plugin bundles (--plugin): one folder per harness, in the place that harness discovers plugins,
# holding skills/, the rendered agents and that harness's manifest. The folder is ours when it
# carries MARKER_FILE; it is rebuilt from scratch on every install so removed skills disappear.
PLUGIN_NAME="ai-agents"
# Manifests carry the newest vX.Y.Z tag; Gemini CLI requires a version field.
VERSION="$(git -C "$REPO" describe --tags --abbrev=0 --match 'v[0-9]*' 2>/dev/null | sed 's/^v//' || true)"
VERSION="${VERSION:-0.1.0}"

plugin_layout() {  # plugin_layout <harness>: sets PD (plugin folder), MANIFEST (inside PD), LOOSE_SK
  # (where a loose install of the same harness keeps skills) and, for codex, MKT/MKT_REL.
  local root
  MKT=""; MKT_REL=""
  case "$1" in
    claude)
      if [ "$SCOPE" = user ]; then LOOSE_SK="$HOME/.claude/skills"; else LOOSE_SK="$TARGET/.claude/skills"; fi
      PD="$LOOSE_SK/$PLUGIN_NAME"; MANIFEST=".claude-plugin/plugin.json" ;;
    antigravity)
      if [ "$SCOPE" = user ]; then PD="$HOME/.gemini/config/plugins/$PLUGIN_NAME"; LOOSE_SK="$HOME/.gemini/config/skills"
      else PD="$TARGET/.agents/plugins/$PLUGIN_NAME"; LOOSE_SK="$TARGET/.agents/skills"; fi
      MANIFEST="plugin.json" ;;
    gemini)
      PD="$HOME/.gemini/extensions/$PLUGIN_NAME"; LOOSE_SK="$HOME/.agents/skills"; MANIFEST="gemini-extension.json" ;;
    copilot)
      if [ "$SCOPE" = user ]; then PD="$HOME/.copilot/plugins/$PLUGIN_NAME"; LOOSE_SK="$HOME/.copilot/skills"
      else PD="$TARGET/.github/plugins/$PLUGIN_NAME"; LOOSE_SK="$TARGET/.github/skills"; fi
      MANIFEST="plugin.json" ;;
    codex)
      # Codex marketplace entries point at ./plugins/<name> relative to the marketplace root.
      if [ "$SCOPE" = user ]; then root="$HOME"; else root="$TARGET"; fi
      PD="$root/plugins/$PLUGIN_NAME"; LOOSE_SK="$root/.agents/skills"; MANIFEST=".codex-plugin/plugin.json"
      MKT="$root/.agents/plugins/marketplace.json"; MKT_REL="./plugins/$PLUGIN_NAME" ;;
    *) echo "unknown harness: $1" >&2; exit 1 ;;
  esac
}

guard_plugin() {  # stop before replacing a plugin folder we did not create
  { [ -e "$1" ] || [ -L "$1" ]; } || return 0
  marked_dir "$1" && return 0
  [ "$FORCE" = 1 ] && { log "force  replacing $1 (not installed by this tool)"; return 0; }
  refuse "$1"
}

plugin_next_step() {  # what the developer has to do before the plugin shows up
  case "$1" in
    claude)      log "next   loads as $PLUGIN_NAME@skills-dir in the next session (or /reload-plugins in a running one)" ;;
    antigravity) log "next   restart Antigravity (IDE extension or agy); new plugin folders are discovered on startup" ;;
    gemini)      log "next   restart Gemini CLI / Gemini Code Assist; check with: gemini extensions list" ;;
    copilot)
      if [ "$SCOPE" = user ]; then log "next   VS Code user settings: \"chat.pluginLocations\": { \"$PD\": true }"
      else log "next   VS Code .vscode/settings.json: \"chat.pluginLocations\": { \".github/plugins/$PLUGIN_NAME\": true }"; fi ;;
    codex)       log "next   in Codex open /plugins and install $PLUGIN_NAME from the \"Local plugins\" marketplace" ;;
  esac
  local s
  for s in $SKILLS; do
    if symlink_into_repo "$LOOSE_SK/$s" || marked_dir "$LOOSE_SK/$s"; then
      log "note   loose skills from an earlier install are still in $LOOSE_SK and will show up twice;"
      log "       remove them with the same command plus --uninstall and without --plugin"
      break
    fi
  done
}

install_plugin() {  # install_plugin <harness>
  local h="$1" s a prefix out r
  plugin_layout "$h"
  guard_plugin "$PD"
  [ "$DRY" = 1 ] && return 0
  rm -rf "$PD"
  mkdir -p "$PD/skills" "$(dirname "$PD/$MANIFEST")"
  printf '%s\n' "$REPO" > "$PD/$MARKER_FILE"   # proves we wrote this folder
  python3 "$RENDER" plugin-manifest "$h" "$VERSION" > "$PD/$MANIFEST"
  for s in $SKILLS; do
    if [ "$MODE" = copy ]; then cp -R "$SKILLS_SRC/$s" "$PD/skills/$s"; else ln -s "$SKILLS_SRC/$s" "$PD/skills/$s"; fi
  done
  # Antigravity agents name their skills by path: absolute for user scope, workspace-relative otherwise.
  if [ "$SCOPE" = user ]; then prefix="$PD/skills"; else prefix=".agents/plugins/$PLUGIN_NAME/skills"; fi
  for a in $AGENTS; do
    case "$h" in
      claude)       mkdir -p "$PD/agents"; out="$PD/agents/$a.md"; r=claude ;;
      antigravity)  mkdir -p "$PD/agents"; out="$PD/agents/$a.md"; r=antigravity ;;
      copilot)      mkdir -p "$PD/agents"; out="$PD/agents/$a.agent.md"; r=copilot ;;
      codex|gemini) mkdir -p "$PD/skills/$a"; out="$PD/skills/$a/SKILL.md"; r=skill ;;  # persona skills
    esac
    if [ "$r" = antigravity ]; then
      { python3 "$RENDER" antigravity "$AGENTS_SRC/$a/AGENT.md" "$prefix"; printf '%s\n' "$MARKER"; } > "$out"
    else
      { python3 "$RENDER" "$r" "$AGENTS_SRC/$a/AGENT.md"; printf '%s\n' "$MARKER"; } > "$out"
    fi
  done
  if [ "$h" = claude ] && has_code_graph; then
    # session start: build the code graph in the background (ASTGRAPH_AUTOBUILD=0 turns it off)
    mkdir -p "$PD/hooks"; python3 "$RENDER" claude-hook plugin > "$PD/hooks/hooks.json"
    log "hook   $PD/hooks/hooks.json (SessionStart: background code-graph build)"
  fi
  log "plugin $PD  <- $(echo "$SKILLS" | wc -w | tr -d ' ') skills, $(echo "$AGENTS" | wc -w | tr -d ' ') agents ($( [ "$MODE" = copy ] && echo copy || echo symlink ))"
  if [ -n "$MKT" ]; then python3 "$RENDER" codex-marketplace add "$MKT" "$MKT_REL"; log "wrote  $MKT (entry $PLUGIN_NAME)"; fi
  plugin_next_step "$h"
  if [ "$h" = antigravity ] && ij="$(intellij_antigravity)"; then
    log "warn   IntelliJ's Antigravity agent found ($ij): it loads neither plugins nor custom agents,"
    log "       so nothing from this plugin shows up in IntelliJ. For IntelliJ, re-run this command with"
    log "       --uninstall, then install without --plugin and with --agents-as-skills"
  fi
}

remove_plugin() {  # remove_plugin <harness>: only a folder carrying our marker is removed
  plugin_layout "$1"
  if [ -e "$PD" ] || [ -L "$PD" ]; then
    if marked_dir "$PD" || [ "$FORCE" = 1 ]; then rm -rf "$PD"; log "removed $PD"
    else log "kept   $PD (not installed by this tool)"; fi
  fi
  if [ -n "$MKT" ] && [ -f "$MKT" ]; then python3 "$RENDER" codex-marketplace remove "$MKT"; log "removed entry $PLUGIN_NAME from $MKT"; fi
  remove_empty_dirs "$(dirname "$PD")"
  # codex: the folder above plugins/ is the project root or HOME itself, never a candidate
  if [ -n "$MKT" ]; then remove_empty_dirs "$(dirname "$MKT")" "$(dirname "$(dirname "$MKT")")"
  else remove_empty_dirs "$(dirname "$(dirname "$PD")")"; fi
}

loose_layout() {  # loose_layout <harness>: sets SK (skills dir), AG (agents dir; empty when the
  # harness has no agent files) and PREFIX (Antigravity's `skills:` path prefix) for $SCOPE
  AG=""; PREFIX=""
  case "$1" in
    claude)
      if [ "$SCOPE" = user ]; then SK="$HOME/.claude/skills"; AG="$HOME/.claude/agents"; else SK="$TARGET/.claude/skills"; AG="$TARGET/.claude/agents"; fi ;;
    codex)
      if [ "$SCOPE" = user ]; then SK="$HOME/.agents/skills"; else SK="$TARGET/.agents/skills"; fi ;;
    antigravity)
      # Native custom agents: .agents/agents/<a>/agent.md; `skills:` entries are paths to the
      # installed skill folders (workspace-relative for project scope, absolute for user scope).
      if [ "$SCOPE" = user ]; then SK="$HOME/.gemini/config/skills"; AG="$HOME/.gemini/config/agents"; PREFIX="$SK"
      else SK="$TARGET/.agents/skills"; AG="$TARGET/.agents/agents"; PREFIX=".agents/skills"; fi ;;
    gemini)
      # User scope: ~/.agents/skills, which Gemini CLI also reads and prefers over ~/.gemini/skills
      # (each duplicate there prints "Skill conflict detected"). One copy, shared with Codex.
      if [ "$SCOPE" = user ]; then SK="$HOME/.agents/skills"; else SK="$TARGET/.gemini/skills"; fi ;;
    copilot)
      if [ "$SCOPE" = user ]; then SK="$HOME/.copilot/skills"; AG="$HOME/.copilot/agents"; else SK="$TARGET/.github/skills"; AG="$TARGET/.github/agents"; fi ;;
    *) echo "unknown harness: $1" >&2; exit 1 ;;
  esac
}

agent_file() {  # agent_file <harness> <agent>: where a loose install keeps that agent (after loose_layout)
  case "$1" in
    claude) printf '%s\n' "$AG/$2.md" ;;
    copilot) printf '%s\n' "$AG/$2.agent.md" ;;
    antigravity) printf '%s\n' "$AG/$2/agent.md" ;;
    codex|gemini) printf '%s\n' "$SK/$2/SKILL.md" ;;   # persona skill
  esac
}

GEMINI_LEGACY_SK="$HOME/.gemini/skills"   # Gemini CLI user installs before they moved to ~/.agents/skills

remove_gemini_legacy() {  # remove our own entries from the old Gemini location; anything else stays
  local s a
  [ -d "$GEMINI_LEGACY_SK" ] || return 0
  for s in $SKILLS; do ours_skill "$GEMINI_LEGACY_SK/$s" && { rm -rf "${GEMINI_LEGACY_SK:?}/$s"; log "removed $GEMINI_LEGACY_SK/$s (moved to $HOME/.agents/skills)"; }; done
  for a in $AGENTS; do
    marked_file "$GEMINI_LEGACY_SK/$a/SKILL.md" || continue
    rm -f "$GEMINI_LEGACY_SK/$a/SKILL.md"; rmdir "$GEMINI_LEGACY_SK/$a" 2>/dev/null || true
    log "removed $GEMINI_LEGACY_SK/$a (moved to $HOME/.agents/skills)"
  done
  remove_empty_dirs "$GEMINI_LEGACY_SK"
}

has_code_graph() { case " $SKILLS " in *" code-graph "*) return 0 ;; esac; return 1; }

claude_settings() {  # where a loose Claude install keeps its hook: user settings, or the project's
  # git-ignored settings.local.json (never the team's committed settings.json)
  if [ "$SCOPE" = user ]; then printf '%s\n' "$HOME/.claude/settings.json"; else printf '%s\n' "$TARGET/.claude/settings.local.json"; fi
}

run_harnesses() {
  for h in ${HARNESSES//,/ }; do
    echo "[$h] scope=$SCOPE target=$TARGET$( [ "$PLUGIN" = 1 ] && echo ' (plugin)')"
    if [ "$PLUGIN" = 1 ]; then
      if [ "$h" = gemini ] && [ "$SCOPE" = project ]; then
        # Gemini CLI reads extensions from ~/.gemini/extensions only; there is no project location.
        [ "$ALL" = 1 ] && { log "skip   gemini has no project-scope plugins (extensions are user scope only)"; continue; }
        echo "gemini: Gemini CLI loads extensions only from ~/.gemini/extensions; use --scope user with --plugin" >&2; exit 1
      fi
      if [ $UNINSTALL = 1 ]; then remove_plugin "$h"; else install_plugin "$h"; fi
      if [ "$h" = claude ] && [ "$SCOPE" = project ] && [ "$DRY" = 0 ]; then
        if [ $UNINSTALL = 1 ]; then remove_import "$TARGET/CLAUDE.md" "@AGENTS.md"; else ensure_import "$TARGET/CLAUDE.md" "@AGENTS.md"; fi
      fi
      continue
    fi
    case "$h" in
      claude)
        loose_layout claude
        if [ $UNINSTALL = 1 ]; then
          for s in $SKILLS; do remove_path "$SK/$s"; done; for a in $AGENTS; do remove_agent claude "$a" "$AG/$a.md"; done
          if has_code_graph && [ -f "$(claude_settings)" ]; then
            python3 "$RENDER" claude-hook remove "$(claude_settings)"; log "removed SessionStart hook from $(claude_settings)"
          fi
          [ "$SCOPE" = project ] && remove_import "$TARGET/CLAUDE.md" "@AGENTS.md"
          remove_empty_dirs "$SK" "$AG" "$(dirname "$SK")"
        else
          for s in $SKILLS; do place_skill "$s" "$SK"; done
          for a in $AGENTS; do write_rendered claude "$a" "$AG/$a.md"; done
          if has_code_graph && [ "$DRY" = 0 ]; then
            # session start: build the code graph in the background (ASTGRAPH_AUTOBUILD=0 turns it off)
            python3 "$RENDER" claude-hook add "$(claude_settings)" "\"$SK/code-graph/scripts/autobuild.sh\""
            log "hook   $(claude_settings) (SessionStart: background code-graph build)"
          fi
          [ "$SCOPE" = project ] && ensure_import "$TARGET/CLAUDE.md" "@AGENTS.md"
        fi ;;
      codex)
        loose_layout codex
        if [ $UNINSTALL = 1 ]; then
          for s in $SKILLS; do remove_path "$SK/$s"; done; for a in $AGENTS; do remove_agent skill "$a" "$SK/$a/SKILL.md"; rmdir "$SK/$a" 2>/dev/null || true; done
          remove_empty_dirs ${AG:+"$AG"} "$SK" "$(dirname "$SK")"
        else
          for s in $SKILLS; do place_skill "$s" "$SK"; done
          for a in $AGENTS; do write_rendered skill "$a" "$SK/$a/SKILL.md"; done
        fi ;;
      antigravity)
        loose_layout antigravity
        if [ $UNINSTALL = 1 ]; then
          for s in $SKILLS; do remove_path "$SK/$s"; done; for a in $AGENTS; do remove_agent antigravity "$a" "$AG/$a" "$PREFIX"; done
          # agents installed as skills (--agents-as-skills): removed whether or not the flag is
          # repeated, and only when the file carries our marker
          for a in $AGENTS; do
            [ -f "$SK/$a/SKILL.md" ] || continue
            remove_agent skill "$a" "$SK/$a/SKILL.md"; rmdir "$SK/$a" 2>/dev/null || true
          done
          remove_empty_dirs "$AG" "$SK" "$(dirname "$SK")"
        else
          for s in $SKILLS; do place_skill "$s" "$SK"; done
          for a in $AGENTS; do write_rendered antigravity "$a" "$AG/$a/agent.md" "$PREFIX"; done
          if [ "$AGENT_SKILLS" = 1 ]; then
            for a in $AGENTS; do write_rendered skill "$a" "$SK/$a/SKILL.md"; done
          elif [ "$DRY" = 0 ] && ij="$(intellij_antigravity)"; then
            log "note   IntelliJ's Antigravity agent found ($ij): it loads skills but not custom agents;"
            log "       re-run with --agents-as-skills to reach the agents there as skills"
          fi
        fi ;;
      gemini)
        loose_layout gemini
        if [ $UNINSTALL = 1 ]; then
          for s in $SKILLS; do remove_path "$SK/$s"; done; for a in $AGENTS; do remove_agent skill "$a" "$SK/$a/SKILL.md"; rmdir "$SK/$a" 2>/dev/null || true; done
          [ "$SCOPE" = project ] && remove_import "$TARGET/GEMINI.md" "@AGENTS.md"
          [ "$SCOPE" = user ] && remove_gemini_legacy
          remove_empty_dirs "$SK" "$(dirname "$SK")"
        else
          for s in $SKILLS; do place_skill "$s" "$SK"; done
          for a in $AGENTS; do write_rendered skill "$a" "$SK/$a/SKILL.md"; done
          [ "$SCOPE" = project ] && ensure_import "$TARGET/GEMINI.md" "@AGENTS.md"
          [ "$SCOPE" = user ] && [ "$DRY" = 0 ] && remove_gemini_legacy
        fi ;;
      copilot)
        loose_layout copilot
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

# ---------------------------------------------------------------------------------------------
# --update: find what this clone installed and re-run each install with the same options. Like
# ownership, this is read from the files (links into this clone, our markers); nothing is stored.
ALL_AGENTS="$(for d in "$AGENTS_SRC"/*/; do basename "$d"; done | tr "\n" " ")"
ALL_SKILLS="$(for d in "$SKILLS_SRC"/*/; do basename "$d"; done | tr "\n" " ")"

ours_here() {  # a skill entry this clone installed: a symlink into it, or a copy it marked
  symlink_into_repo "$1" && return 0
  marked_dir "$1" && [ "$(cat "$1/$MARKER_FILE" 2>/dev/null)" = "$REPO" ]
}

# shellcheck disable=SC2034  # D_PLUGIN/D_MODE/D_AS are read through eval in the --update driver
detect_install() {  # detect_install <harness>: sets D_PLUGIN D_MODE D_AGENTS D_SKILLS D_AS D_OTHER
  local h="$1" s a f
  D_PLUGIN=0; D_MODE="link"; D_AGENTS=""; D_SKILLS=""; D_AS=0; D_OTHER=""; D_UNMARKED=""
  if ! { [ "$h" = gemini ] && [ "$SCOPE" = project ]; }; then
    plugin_layout "$h"
    if marked_dir "$PD"; then
      if [ "$(cat "$PD/$MARKER_FILE")" != "$REPO" ]; then D_OTHER="$(cat "$PD/$MARKER_FILE")"; return 0; fi
      D_PLUGIN=1
      for s in $ALL_SKILLS; do
        { [ -e "$PD/skills/$s" ] || [ -L "$PD/skills/$s" ]; } || continue
        D_SKILLS="$D_SKILLS $s"; [ -L "$PD/skills/$s" ] || D_MODE=copy
      done
      for a in $ALL_AGENTS; do
        case "$h" in
          claude|antigravity) f="$PD/agents/$a.md" ;;
          copilot) f="$PD/agents/$a.agent.md" ;;
          *) f="$PD/skills/$a/SKILL.md" ;;
        esac
        [ -f "$f" ] && D_AGENTS="$D_AGENTS $a"
      done
      return 0
    fi
  fi
  loose_layout "$h"
  # a Gemini user install made before the move to ~/.agents/skills is still found (and then moved)
  if [ "$h" = gemini ] && [ "$SCOPE" = user ] && [ -d "$GEMINI_LEGACY_SK" ] && ! ours_here "$SK/code-graph"; then
    for s in $ALL_SKILLS; do ours_here "$GEMINI_LEGACY_SK/$s" && { SK="$GEMINI_LEGACY_SK"; break; }; done
  fi
  for s in $ALL_SKILLS; do
    if ours_here "$SK/$s"; then
      D_SKILLS="$D_SKILLS $s"; [ -L "$SK/$s" ] || D_MODE=copy
    elif [ -L "$SK/$s" ] && [ -f "$(resolve_link "$SK/$s")/../../install.sh" ]; then
      D_OTHER="$(cd "$(resolve_link "$SK/$s")/../.." && pwd)"   # linked into another clone
    fi
  done
  for a in $ALL_AGENTS; do
    f="$(agent_file "$h" "$a")"
    if marked_file "$f"; then D_AGENTS="$D_AGENTS $a"
    elif [ -f "$f" ]; then
      # an agent file without our marker: an install older than the marker, or the developer's
      # own file. Taken over only with --force; otherwise named so the developer can decide.
      if [ "$FORCE" = 1 ]; then D_AGENTS="$D_AGENTS $a"; TAKEOVER_FILES="${TAKEOVER_FILES:-} $f"
      else D_UNMARKED="$D_UNMARKED $f"; fi
    fi
    [ "$h" = antigravity ] && marked_file "$SK/$a/SKILL.md" && D_AS=1
  done
  # agents alone do not prove the install came from this clone (rendered files name no clone)
  [ -n "$D_SKILLS" ] || D_AGENTS=""
  return 0
}

prune_stale() {  # prune_stale <harness>: remove our loose files for agents/skills deleted upstream
  local h="$1" e n
  loose_layout "$h"
  for e in "$SK"/* "$SK"/.[!.]*; do
    { [ -e "$e" ] || [ -L "$e" ]; } || continue
    n="$(basename "$e")"
    case " $ALL_SKILLS $ALL_AGENTS " in *" $n "*) continue ;; esac
    # a link or marked copy this clone installed, or an agent we rendered as a skill
    if ours_here "$e" || marked_file "$e/SKILL.md"; then
      rm -rf "$e"; log "removed $e (no longer in the repo)"
    fi
  done
  [ -n "$AG" ] && [ -d "$AG" ] || return 0
  for e in "$AG"/*; do
    [ -e "$e" ] || continue
    n="$(basename "$e")"; n="${n%.agent.md}"; n="${n%.md}"
    case " $ALL_AGENTS " in *" $n "*) continue ;; esac
    if marked_file "$e" || marked_file "$e/agent.md"; then rm -rf "$e"; log "removed $e (no longer in the repo)"; fi
  done
}

with_declared_skills() {  # print skill list $2 plus every skill the agents in $1 declare
  local out="$2" a s
  for a in $1; do
    for s in $(sed -n 's/^skills: *\[\(.*\)\]/\1/p' "$AGENTS_SRC/$a/AGENT.md" | tr ',' ' '); do
      case " $out " in *" $s "*) ;; *) out="$out $s" ;; esac
    done
  done
  printf '%s\n' "$out"
}

missing_from() {  # missing_from <installed> <available>: names in available but not installed
  local n out=""
  for n in $2; do case " $1 " in *" $n "*) ;; *) out="$out $n" ;; esac; done
  printf '%s\n' "${out# }"
}

if [ "$UPDATE" = 1 ]; then
  echo "update: scope=$SCOPE$( [ "$SCOPE" = project ] && echo " target=$TARGET") from $REPO"
  FOUND=""
  for h in ${HARNESSES//,/ }; do
    detect_install "$h"
    if [ -n "$D_OTHER" ] && [ -z "$D_SKILLS" ]; then
      log "skip   $h: installed from another clone ($D_OTHER); run --update there, or uninstall it there and install from here"
      continue
    fi
    [ -n "$D_SKILLS$D_AGENTS" ] || continue
    for f in $D_UNMARKED; do
      log "keep   $f: no installer marker (an install older than the marker, or your own file);"
      log "       not updated. If it is ours, add --force to take it over"
    done
    FOUND="$FOUND $h"
    # bash 3.2 has no associative arrays: one variable set per harness
    eval "U_${h}_PLUGIN=\$D_PLUGIN U_${h}_MODE=\$D_MODE U_${h}_AGENTS=\$D_AGENTS U_${h}_SKILLS=\$D_SKILLS U_${h}_AS=\$D_AS"
  done
  # During --update, --force only takes over the unmarked agent files detection named
  # (TAKEOVER_FILES); every other path keeps the normal guard, so a developer's own same-named
  # skill is never replaced.
  FORCE=0
  [ -n "$FOUND" ] || { echo "update: nothing installed from this clone at scope=$SCOPE; install first (see --help)"; exit 0; }
  use_detected() {  # load the stored detection for harness $1 into the variables run_harnesses reads
    eval "PLUGIN=\$U_${1}_PLUGIN MODE=\$U_${1}_MODE AGENTS=\$U_${1}_AGENTS AGENT_SKILLS=\$U_${1}_AS"
    eval "SKILLS=\$U_${1}_SKILLS"
    SKILLS="$(with_declared_skills "$AGENTS" "$SKILLS")"
    HARNESSES="$1"
  }
  # Pre-flight every install first, so a clash in one cannot leave the others half-updated.
  DRY=1; for h in $FOUND; do use_detected "$h"; run_harnesses > /dev/null; done; DRY=0
  for h in $FOUND; do
    use_detected "$h"
    run_harnesses
    [ "$PLUGIN" = 1 ] || prune_stale "$h"   # plugins are rebuilt whole; loose installs need pruning
    extra_a="$(missing_from "$AGENTS" "$ALL_AGENTS")"
    extra_s="$(missing_from "$SKILLS" "$(missing_from install-agents "$ALL_SKILLS")")"
    if [ -n "$extra_a$extra_s" ]; then
      log "note   in the repo but not in this install:${extra_a:+ agents: $extra_a;}${extra_s:+ skills: $extra_s}"
      log "       add them by re-installing with the lists you want (--agents/--skills), or without them for everything"
    fi
  done
  [ "$SCOPE" = project ] && update_agents_md
  echo "done (update: ${FOUND# })"
  exit 0
fi

# Pre-flight: check every destination before writing anything, so a clash cannot leave a
# half-finished install behind.
if [ $UNINSTALL = 0 ]; then DRY=1; run_harnesses > /dev/null; DRY=0; fi
run_harnesses

if [ "$SCOPE" = project ]; then
  if [ $UNINSTALL = 1 ]; then remove_agents_md_block; else update_agents_md; fi
fi
[ $UNINSTALL = 1 ] && echo "done (uninstall)" || echo "done. Add '.ast-graph/' to the target's .gitignore; the graph is a build artifact."
