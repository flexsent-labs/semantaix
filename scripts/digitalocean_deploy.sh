#!/usr/bin/env bash
# Redeploy Semantaix on an existing DigitalOcean host (after bootstrap finish).
#
# Manual (git pull on server):
#   sudo bash /opt/semantaix/scripts/digitalocean_deploy.sh
#
# GitHub Actions (code already rsync'd; no git):
#   bash /opt/semantaix/scripts/digitalocean_deploy.sh --no-git \
#     --services "api bot_gateway user_gateway nginx"
set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/opt/semantaix}"
BRANCH="${BRANCH:-main}"
SKIP_GIT=0
SERVICES=""

log() { printf '\n=== %s ===\n' "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage:
  digitalocean_deploy.sh [--dir /opt/semantaix] [--branch main]
  digitalocean_deploy.sh --no-git [--services "api bot_gateway user_gateway nginx"]

  --no-git     Skip git fetch/merge (GitHub Actions rsync deploy)
  --services   Space-separated compose service names (default: all services)
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DEPLOY_DIR="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --no-git) SKIP_GIT=1; shift ;;
    --services) SERVICES="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ -f "$DEPLOY_DIR/docker-compose.yml" ] || die "no compose file in $DEPLOY_DIR"
[ -f "$DEPLOY_DIR/docker-compose.prod.yml" ] || die "missing $DEPLOY_DIR/docker-compose.prod.yml"

compose() {
  docker compose -f "$DEPLOY_DIR/docker-compose.yml" \
    -f "$DEPLOY_DIR/docker-compose.prod.yml" \
    --project-directory "$DEPLOY_DIR" "$@"
}

require_docker_access() {
  if [ "$(id -u)" -eq 0 ]; then
    return 0
  fi
  if groups | tr ' ' '\n' | grep -qx docker; then
    return 0
  fi
  die "run as root or as a user in the docker group"
}

require_docker_access

if [ "$SKIP_GIT" -eq 0 ]; then
  log "git pull $BRANCH"
  git -C "$DEPLOY_DIR" fetch origin "$BRANCH"
  if ! git -C "$DEPLOY_DIR" diff --quiet || ! git -C "$DEPLOY_DIR" diff --cached --quiet; then
    die "$DEPLOY_DIR has local changes — commit/stash before deploy"
  fi
  git -C "$DEPLOY_DIR" checkout "$BRANCH" 2>/dev/null || true
  git -C "$DEPLOY_DIR" merge --ff-only "origin/$BRANCH"
  git -C "$DEPLOY_DIR" --no-pager log --oneline -1
else
  log "skip git (--no-git)"
fi

log "build + up"
compose build
if [ -n "$SERVICES" ]; then
  # shellcheck disable=SC2086
  compose up -d $SERVICES
else
  compose up -d
fi
compose restart nginx

code=""
for _ in $(seq 1 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/telegram/webhook || true)"
  [ "$code" = "405" ] && break
  sleep 2
done
api="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/api/health/live || true)"
printf 'GET http://127.0.0.1:8080/telegram/webhook -> %s (expect 405)\n' "$code"
printf 'GET http://127.0.0.1:8080/api/health/live  -> %s (expect 200)\n' "$api"
[ "$code" = "405" ] || die "webhook unhealthy ($code) — see docker compose logs nginx"
log "OK"
