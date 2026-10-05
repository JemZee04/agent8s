#!/usr/bin/env bash
# Builds the phone UI and (re)deploys the relay to a server you can `ssh` into.
#   PROXY_NETWORK=mysite_default relay/deploy.sh myserver
# The server needs Docker with the compose plugin, and an nginx on PROXY_NETWORK that
# forwards /agent8s/ to agent8s-relay:8765 (see "Phone" in the README).
set -euo pipefail

HOST="${1:?usage: PROXY_NETWORK=<docker network of your nginx> relay/deploy.sh <ssh host>}"
: "${PROXY_NETWORK:?set PROXY_NETWORK to the Docker network your nginx is on}"
REMOTE_DIR="${REMOTE_DIR:-/opt/agent8s-relay}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

(cd "$ROOT/desktop-ui" && npm ci --silent && npm run build)

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp "$ROOT/relay/Dockerfile" "$ROOT/relay/docker-compose.yml" "$ROOT/src/agent8s/relay/server.py" "$STAGE/"
mkdir "$STAGE/web"
# The relay only serves the UI; nothing else from the bundle is needed.
cp -R "$ROOT/src/agent8s/desktop/web/." "$STAGE/web/"

ssh "$HOST" "mkdir -p '$REMOTE_DIR'"
rsync -az --delete "$STAGE/" "$HOST:$REMOTE_DIR/"
ssh "$HOST" "cd '$REMOTE_DIR' && echo 'PROXY_NETWORK=$PROXY_NETWORK' > .env && docker compose up -d --build --remove-orphans && docker image prune -f >/dev/null"
echo "relay deployed to $HOST:$REMOTE_DIR"
