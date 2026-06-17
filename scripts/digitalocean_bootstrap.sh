#!/usr/bin/env bash
# One-time DigitalOcean droplet bootstrap: Docker, clone, TLS, first deploy.
#
# Run on a fresh Ubuntu 22.04/24.04 droplet as root (or via sudo):
#
#   curl -fsSL https://raw.githubusercontent.com/flexsent-labs/semantaix/main/scripts/digitalocean_bootstrap.sh \
#     | sudo bash -s -- \
#       --domain semantaix.example.com \
#       --email you@example.com \
#       --repo https://github.com/flexsent-labs/semantaix.git
#
# Then edit secrets and finish:
#
#   sudo nano /opt/semantaix/.env
#   sudo bash /opt/semantaix/scripts/digitalocean_bootstrap.sh finish \
#     --domain semantaix.example.com \
#     --email you@example.com
#
# Day-2 redeploys: scripts/digitalocean_deploy.sh
set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/opt/semantaix}"
REPO_URL="${REPO_URL:-https://github.com/flexsent-labs/semantaix.git}"
BRANCH="${BRANCH:-main}"
DOMAIN=""
EMAIL=""
PHASE="prepare"
CLOUDFLARE=0

log() { printf '\n=== %s ===\n' "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

cloudflare_notes() {
  cat <<EOF
Cloudflare (semantaix.flexsentlabs.com):
  • DNS: A record  semantaix  →  <droplet IPv4>  (or A  semantaix.flexsentlabs.com)
  • First deploy: set the record to **DNS only** (grey cloud) until 'finish' succeeds.
  • After HTTPS works: SSL/TLS mode **Full (strict)**, then enable **Proxied** (orange).
  • Do not use SSL mode "Flexible" — Telegram webhooks need HTTPS end-to-end.
EOF
}

usage() {
  cat <<'EOF'
Usage:
  digitalocean_bootstrap.sh prepare --domain FQDN --email EMAIL [--repo URL] [--dir PATH]
  digitalocean_bootstrap.sh finish  --domain FQDN --email EMAIL [--dir PATH]

  prepare  Install Docker, nginx, certbot; clone repo; scaffold .env + host nginx.
  finish   After you edit .env secrets: build stack, obtain TLS cert, set webhook.

Required flags:
  --domain   Public DNS name (A record → this droplet), e.g. semantaix.example.com
  --email    Let's Encrypt registration email for certbot

Optional:
  --repo     Git remote (default: flexsent-labs/semantaix)
  --dir      Deploy directory (default: /opt/semantaix)
  --branch   Git branch (default: main)
  --cloudflare   Print Cloudflare DNS/TLS checklist (use for semantaix.flexsentlabs.com)
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    prepare|finish) PHASE="$1"; shift ;;
    --domain) DOMAIN="$2"; shift 2 ;;
    --email) EMAIL="$2"; shift 2 ;;
    --repo) REPO_URL="$2"; shift 2 ;;
    --dir) DEPLOY_DIR="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --cloudflare) CLOUDFLARE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

[ -n "$DOMAIN" ] || die "--domain is required"
[ -n "$EMAIL" ] || die "--email is required"

if [ "$(id -u)" -ne 0 ]; then
  die "run as root (sudo bash $0 ...)"
fi

compose() {
  docker compose -f "$DEPLOY_DIR/docker-compose.yml" \
    -f "$DEPLOY_DIR/docker-compose.prod.yml" \
    --project-directory "$DEPLOY_DIR" "$@"
}

require_env_ready() {
  local env_file="$DEPLOY_DIR/.env"
  [ -f "$env_file" ] || die "missing $env_file — run prepare first"
  if grep -qE '^(TELEGRAM_BOT_TOKEN|OPENROUTER_API_KEY)=replace-me' "$env_file"; then
    die "edit $env_file — set TELEGRAM_BOT_TOKEN and OPENROUTER_API_KEY (and other secrets)"
  fi
  if ! grep -q '^INTERNAL_SERVICE_TOKEN=.' "$env_file"; then
    die "INTERNAL_SERVICE_TOKEN missing in $env_file"
  fi
}

render_host_nginx() {
  local site="/etc/nginx/sites-available/semantaix"
  export DOMAIN
  envsubst '${DOMAIN}' < "$DEPLOY_DIR/infra/nginx/host-tls.conf.template" > "$site"
  ln -sf "$site" /etc/nginx/sites-enabled/semantaix
  rm -f /etc/nginx/sites-enabled/default
  nginx -t
  systemctl enable nginx
  systemctl reload nginx
}

install_packages() {
  log "apt update + base packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl git ufw nginx certbot python3-certbot-nginx \
    gettext-base

  if ! command -v docker >/dev/null 2>&1; then
    log "install Docker"
    curl -fsSL https://get.docker.com | sh
  fi
  systemctl enable docker
  systemctl start docker

  log "firewall (OpenSSH + Nginx Full)"
  ufw allow OpenSSH
  ufw allow 'Nginx Full'
  ufw --force enable
}

clone_or_update() {
  if [ -d "$DEPLOY_DIR/.git" ]; then
    log "git pull $BRANCH in $DEPLOY_DIR"
    git -C "$DEPLOY_DIR" fetch origin "$BRANCH"
    git -C "$DEPLOY_DIR" checkout "$BRANCH"
    git -C "$DEPLOY_DIR" pull --ff-only origin "$BRANCH"
  else
    log "git clone → $DEPLOY_DIR"
    mkdir -p "$(dirname "$DEPLOY_DIR")"
    git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$DEPLOY_DIR"
  fi
}

scaffold_env() {
  local env_file="$DEPLOY_DIR/.env"
  if [ ! -f "$env_file" ]; then
    cp "$DEPLOY_DIR/.env.example" "$env_file"
  fi
  local token
  token="$(openssl rand -hex 32)"
  grep -q '^APP_ENV=' "$env_file" \
    && sed -i "s/^APP_ENV=.*/APP_ENV=production/" "$env_file" \
    || echo "APP_ENV=production" >> "$env_file"
  grep -q '^LOG_FORMAT=' "$env_file" \
    && sed -i 's/^LOG_FORMAT=.*/LOG_FORMAT=json/' "$env_file" \
    || echo "LOG_FORMAT=json" >> "$env_file"
  if ! grep -q '^INTERNAL_SERVICE_TOKEN=' "$env_file"; then
    echo "INTERNAL_SERVICE_TOKEN=$token" >> "$env_file"
  elif grep -q '^INTERNAL_SERVICE_TOKEN=$' "$env_file"; then
    sed -i "s/^INTERNAL_SERVICE_TOKEN=.*/INTERNAL_SERVICE_TOKEN=$token/" "$env_file"
  fi
  if grep -q '^WEB_UI_BASE_URL=' "$env_file"; then
    sed -i "s|^WEB_UI_BASE_URL=.*|WEB_UI_BASE_URL=https://${DOMAIN}/admin|" "$env_file"
  else
    echo "WEB_UI_BASE_URL=https://${DOMAIN}/admin" >> "$env_file"
  fi
  if grep -q '^TELEGRAM_PLATFORM_BOT_USERNAME=' "$env_file"; then
    sed -i 's/^TELEGRAM_PLATFORM_BOT_USERNAME=.*/TELEGRAM_PLATFORM_BOT_USERNAME=@semantaix_bot/' \
      "$env_file"
  fi
  local oauth_uri="https://${DOMAIN}/api/calendar/oauth/callback"
  if grep -q '^GOOGLE_OAUTH_REDIRECT_URI=' "$env_file"; then
    sed -i "s|^GOOGLE_OAUTH_REDIRECT_URI=.*|GOOGLE_OAUTH_REDIRECT_URI=${oauth_uri}|" "$env_file"
  else
    echo "GOOGLE_OAUTH_REDIRECT_URI=${oauth_uri}" >> "$env_file"
  fi
  if grep -q '^WEB_UI_ADMIN_COOKIE_SECURE=' "$env_file"; then
    sed -i 's/^WEB_UI_ADMIN_COOKIE_SECURE=.*/WEB_UI_ADMIN_COOKIE_SECURE=true/' "$env_file"
  else
    echo "WEB_UI_ADMIN_COOKIE_SECURE=true" >> "$env_file"
  fi
  # Deprecated since Epic 10.5 — remove if a stale template still has them.
  sed -i '/^HITL_PRIMARY_OPERATOR_USERNAME=/d' "$env_file" || true
  sed -i '/^HITL_PRIMARY_OPERATOR_CHAT_ID=/d' "$env_file" || true
  chmod 600 "$env_file"
  log "scaffolded $env_file (generated INTERNAL_SERVICE_TOKEN; edit other secrets)"
}

prepare_phase() {
  install_packages
  clone_or_update
  scaffold_env
  render_host_nginx
  log "prepare complete"
  local droplet_ip
  droplet_ip="$(curl -fsS https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
  cat <<EOF

Next steps:
  1. Cloudflare DNS:  semantaix.flexsentlabs.com  A  →  ${droplet_ip}
     (grey cloud / DNS only until 'finish' completes — see --cloudflare)
  2. Edit secrets:  nano ${DEPLOY_DIR}/.env
     Required: TELEGRAM_BOT_TOKEN, OPENROUTER_API_KEY, TELEGRAM_ALERT_CHAT_ID,
               HITL_CONFIG_ADMIN_USERNAME, TELEGRAM_API_ID, TELEGRAM_API_HASH (user_gateway)
  3. Finish deploy:  sudo bash ${DEPLOY_DIR}/scripts/digitalocean_bootstrap.sh finish \\
       --domain ${DOMAIN} --email ${EMAIL} --dir ${DEPLOY_DIR} --cloudflare

EOF
  if [ "$CLOUDFLARE" -eq 1 ]; then
    cloudflare_notes
  fi
}

verify_stack() {
  local code api
  for _ in $(seq 1 45); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8080/telegram/webhook" || true)"
    api="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8080/api/health/live" || true)"
    [ "$code" = "405" ] && [ "$api" = "200" ] && break
    sleep 2
  done
  printf 'docker nginx: /telegram/webhook -> %s (expect 405)\n' "$code"
  printf 'docker nginx: /api/health/live  -> %s (expect 200)\n' "$api"
  [ "$code" = "405" ] || die "stack unhealthy (webhook $code)"
}

set_telegram_webhook() {
  # shellcheck disable=SC1091
  set -a && source "$DEPLOY_DIR/.env" && set +a
  [ -n "${TELEGRAM_BOT_TOKEN:-}" ] || die "TELEGRAM_BOT_TOKEN empty"
  local url="https://${DOMAIN}/telegram/webhook"
  log "setWebhook → $url"
  curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
    -d "url=${url}" | python3 -m json.tool
}

finish_phase() {
  require_env_ready
  clone_or_update
  render_host_nginx
  log "docker compose build + up (prod overlay)"
  compose build
  compose up -d
  log "restart in-compose nginx (upstream IP refresh)"
  compose restart nginx
  verify_stack
  log "certbot TLS for $DOMAIN"
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$EMAIL" \
    --redirect || die "certbot failed — check DNS points to this host"
  systemctl reload nginx
  local https_code
  https_code="$(curl -s -o /dev/null -w '%{http_code}' "https://${DOMAIN}/telegram/webhook" || true)"
  printf 'public HTTPS: /telegram/webhook -> %s (expect 405)\n' "$https_code"
  [ "$https_code" = "405" ] || die "HTTPS webhook route unhealthy ($https_code)"
  set_telegram_webhook
  log "deploy complete"
  echo "Admin UI: https://${DOMAIN}/admin"
  echo "Webhook:  https://${DOMAIN}/telegram/webhook"
  echo "Redeploy: sudo bash ${DEPLOY_DIR}/scripts/digitalocean_deploy.sh"
  if [ "$CLOUDFLARE" -eq 1 ]; then
    echo ""
    cloudflare_notes
    echo "  • Confirm https://${DOMAIN}/api/health/live returns 200 through Cloudflare."
  fi
}

case "$PHASE" in
  prepare) prepare_phase ;;
  finish) finish_phase ;;
  *) die "unknown phase: $PHASE" ;;
esac
