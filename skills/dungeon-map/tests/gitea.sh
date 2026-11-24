#!/usr/bin/env bash
# gitea.sh -- a throwaway Gitea in Docker with the test org (tests/org/) for dungeon-map and compass.
#
#   tests/gitea.sh up      # container dm-gitea on localhost:3300, gt logged in, org dmtest seeded
#   tests/gitea.sh relogin SCOPES  # gt logged in again with a new token of only these scopes
#                                  # (e.g. the ones SKILL.md documents for the OAuth login)
#   tests/gitea.sh down    # gt logged out of it, container removed (all its data goes with it)
#
# DM_GITEA_MAX_ITEMS / DM_GITEA_TREE_PAGE lower the server's page caps (MAX_RESPONSE_ITEMS,
# DEFAULT_GIT_TREES_PER_PAGE) so that listings and trees need several pages even for the small org.
#
# The admin token goes from `gitea admin user generate-access-token` straight into
# `gt auth login --with-token` through a pipe: it is never in an environment variable or a file.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME="${DM_GITEA_NAME:-dm-gitea}"
PORT="${DM_GITEA_PORT:-3300}"
IMAGE="${DM_GITEA_IMAGE:-gitea/gitea:1.24}"
HOST="localhost:$PORT"
ORG="${DM_GITEA_ORG:-dmtest}"
MAX_ITEMS="${DM_GITEA_MAX_ITEMS:-50}"
TREE_PAGE="${DM_GITEA_TREE_PAGE:-1000}"

# a new token for dmadmin, piped straight into gt's login
login() {
  docker exec -u git "$NAME" gitea admin user generate-access-token --username dmadmin \
    --token-name "dungeon-map-test-$(date +%s%N)" --scopes "$1" --raw \
    | gt auth login --host "http://$HOST" --with-token --yes
}

up() {
  docker run -d --name "$NAME" -p "127.0.0.1:$PORT:3000" \
    -e GITEA__security__INSTALL_LOCK=true \
    -e GITEA__server__ROOT_URL="http://$HOST/" \
    -e GITEA__server__DOMAIN=localhost \
    -e GITEA__database__DB_TYPE=sqlite3 \
    -e GITEA__repository__DEFAULT_BRANCH=main \
    -e GITEA__api__MAX_RESPONSE_ITEMS="$MAX_ITEMS" \
    -e GITEA__api__DEFAULT_GIT_TREES_PER_PAGE="$TREE_PAGE" \
    "$IMAGE" >/dev/null
  for _ in $(seq 1 60); do
    curl -fsS "http://$HOST/api/v1/version" >/dev/null 2>&1 && break
    sleep 1
  done
  curl -fsS "http://$HOST/api/v1/version"; echo
  docker exec -u git "$NAME" gitea admin user create --admin --username dmadmin \
    --password "dm-$(date +%s)-throwaway" --email dmadmin@example.invalid --must-change-password=false >/dev/null
  login all
  python3 "$HERE/seed_gitea.py" --host "$HOST" --org "$ORG"
  settle
}

# Gitea computes repository languages asynchronously after a push: a build (or --save) made
# right after seeding can miss some. Wait until the listing's languages stop changing.
settle() {
  local prev="" cur
  for _ in $(seq 1 30); do
    cur="$(gt --no-input --host "$HOST" api "orgs/$ORG/repos?limit=50" \
      | python3 -c 'import json,sys; print(sorted((r["name"], r.get("language") or "") for r in json.load(sys.stdin)))')"
    [ -n "$prev" ] && [ "$cur" = "$prev" ] && { echo "languages settled"; return; }
    prev="$cur"
    sleep 3
  done
  echo "languages still changing after 90s; record again later" >&2
}

down() {
  gt auth logout --host "$HOST" --yes 2>/dev/null || gt auth logout --host "http://$HOST" 2>/dev/null || true
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  echo "removed $NAME"
}

relogin() {
  gt auth logout --host "$HOST" --yes >/dev/null 2>&1 || true
  login "$1"
}

case "${1:-}" in
  up) up ;;
  relogin) relogin "${2:?usage: $0 relogin SCOPES}" ;;
  down) down ;;
  *) echo "usage: $0 up|relogin SCOPES|down" >&2; exit 2 ;;
esac
