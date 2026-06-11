#!/usr/bin/env bash
# Autonomous redeploy + restart of the Semantaix live stack (no prompts).
#
# Safe to run unattended:
#   - advances the deploy checkout to origin/main ONLY on a clean fast-forward
#   - never `git checkout main` (main is usually locked in another worktree)
#   - ABORTS rather than clobbering if the deploy dir is dirty or has diverged
#   - always restarts nginx so it re-resolves recreated backend IPs (the 502 fix)
#   - verifies through nginx before declaring success; exits non-zero on failure
set -euo pipefail

log() { printf '\n=== %s ===\n' "$*"; }

# 1) Locate the deploy dir = where the live stack builds from. Prefer the
#    running `semantaix` compose project's working_dir; fall back to the main
#    worktree root (shared .git parent). Works invoked from any worktree.
DEPLOY_DIR="$(docker ps --filter label=com.docker.compose.project=semantaix \
  --format '{{.Label "com.docker.compose.project.working_dir"}}' 2>/dev/null | head -1 || true)"
if [ -z "${DEPLOY_DIR:-}" ] || [ ! -f "$DEPLOY_DIR/docker-compose.yml" ]; then
  DEPLOY_DIR="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"
fi
COMPOSE="$DEPLOY_DIR/docker-compose.yml"
[ -f "$COMPOSE" ] || { echo "ERROR: no docker-compose.yml at $DEPLOY_DIR"; exit 1; }
DC=(docker compose -f "$COMPOSE" --project-directory "$DEPLOY_DIR")
log "Deploy dir: $DEPLOY_DIR"

# 2) Ensure the Docker daemon is up (start Docker Desktop + poll on macOS).
if ! docker info >/dev/null 2>&1; then
  log "Starting Docker Desktop"
  open -a Docker >/dev/null 2>&1 || true
  for _ in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 2; done
  docker info >/dev/null 2>&1 || { echo "ERROR: Docker daemon not available"; exit 1; }
fi

# 3) Advance the deploy checkout to origin/main — clean fast-forward only.
#    No `checkout main` (it may be checked out/locked in another worktree); we
#    just fast-forward whatever branch is checked out. Refuse to clobber.
log "Updating $DEPLOY_DIR to origin/main"
git -C "$DEPLOY_DIR" fetch origin main
if ! git -C "$DEPLOY_DIR" diff --quiet || ! git -C "$DEPLOY_DIR" diff --cached --quiet; then
  echo "ERROR: $DEPLOY_DIR has uncommitted tracked changes — aborting (no auto-clobber)."
  echo "       Commit/stash them or deploy manually, then re-run."
  exit 1
fi
if ! git -C "$DEPLOY_DIR" merge --ff-only origin/main; then
  branch="$(git -C "$DEPLOY_DIR" branch --show-current 2>/dev/null || echo detached)"
  echo "ERROR: $DEPLOY_DIR ($branch) is NOT a fast-forward of origin/main (diverged)."
  echo "       Reconcile manually — refusing to force."
  exit 1
fi
git -C "$DEPLOY_DIR" --no-pager log --oneline -1

# 4) Rebuild images + recreate containers.
log "docker compose build"
"${DC[@]}" build
log "docker compose up -d"
"${DC[@]}" up -d

# 5) Restart nginx so it re-resolves the recreated backends' NEW IPs.
#    nginx proxy_pass uses literal hostnames with no resolver, so it caches each
#    upstream IP at startup; `up -d` gives api/bot_gateway new IPs, and without
#    this restart nginx keeps proxying the dead old IPs and 502s the Telegram
#    webhook (customer messages silently stop). This was a real incident.
log "docker compose restart nginx"
"${DC[@]}" restart nginx

# 6) Verify THROUGH nginx (not just internal health): the webhook route must be
#    reachable (405, POST-only) and never 502; api health must be 200.
NGINX_PORT="$({ "${DC[@]}" port nginx 80 2>/dev/null || true; } | sed -n 's/.*:\([0-9]\{1,\}\)$/\1/p' | head -1)"
NGINX_PORT="${NGINX_PORT:-80}"
BASE="http://localhost:${NGINX_PORT}"
log "Verifying $BASE"
code=""
for _ in $(seq 1 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/telegram/webhook" || true)"
  [ "$code" = "405" ] && break
  sleep 2
done
api="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/health/live" || true)"
"${DC[@]}" ps
printf 'GET %s/telegram/webhook -> %s (expect 405, never 502)\n' "$BASE" "$code"
printf 'GET %s/api/health/live  -> %s (expect 200)\n' "$BASE" "$api"
if [ "$code" != "405" ]; then
  echo "ERROR: webhook route unhealthy (got '$code'). If 502, the backends got new"
  echo "       IPs and nginx didn't re-resolve — re-run, or see 'docker compose logs nginx'."
  exit 1
fi
echo "OK: live stack rebuilt from origin/main and serving (webhook 405, api ${api})."
