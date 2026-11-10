#!/usr/bin/env bash
# Lists every tracked shell script for ShellCheck: *.sh files, plus files whose first line is a
# sh/bash/dash shebang. Scripts installed as commands (skills/ai-task/scripts/ai-task) have no
# extension, so `git ls-files '*.sh'` alone misses them. Only the first line counts, so a shebang
# inside a heredoc or a test string does not make a file a script.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
git ls-files -z | while IFS= read -r -d '' f; do
  case "$f" in
    *.sh) echo "$f" ;;
    *)
      if [ -f "$f" ] && head -n 1 -- "$f" | grep -qE '^#!.*[/ ](ba|da)?sh([[:space:]]|$)'; then
        echo "$f"
      fi
      ;;
  esac
done
