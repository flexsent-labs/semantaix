#!/usr/bin/env bash
# One-time host setup for GitHub Actions deploy (rsync + docker compose).
#
# On the droplet (as root), after bootstrap prepare/finish:
#
#   ssh-keygen -t ed25519 -f ./gha-deploy -N "" -C "semantaix-gha-deploy"
#   sudo bash scripts/digitalocean_setup_github_actions.sh \
#     --pubkey-file ./gha-deploy.pub
#
# Add GitHub repository secrets (Settings → Secrets → Actions):
#   DEPLOY_HOST     = droplet public IPv4
#   DEPLOY_USER     = semantaix-deploy
#   DEPLOY_SSH_KEY  = contents of gha-deploy (private key, including BEGIN/END lines)
#
# Optional repository variables (Settings → Variables → Actions):
#   DEPLOY_DOMAIN   = semantaix.flexsentlabs.com
#   DEPLOY_SERVICES = api bot_gateway user_gateway nginx
#
# Create a GitHub Environment named "production" (Settings → Environments) so
# deploy.yml can gate secrets; optional required reviewers for manual approval.
set -euo pipefail

DEPLOY_USER="${DEPLOY_USER:-semantaix-deploy}"
DEPLOY_DIR="${DEPLOY_DIR:-/opt/semantaix}"
PUBKEY_FILE=""
PUBKEY=""

log() { printf '\n=== %s ===\n' "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage:
  digitalocean_setup_github_actions.sh --pubkey-file PATH
  digitalocean_setup_github_actions.sh --pubkey "ssh-ed25519 AAAA... comment"

Creates user semantaix-deploy (docker group), installs authorized_keys, and
chowns /opt/semantaix for CI rsync deploys.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --pubkey-file) PUBKEY_FILE="$2"; shift 2 ;;
    --pubkey) PUBKEY="$2"; shift 2 ;;
    --user) DEPLOY_USER="$2"; shift 2 ;;
    --dir) DEPLOY_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"

if [ -n "$PUBKEY_FILE" ]; then
  [ -f "$PUBKEY_FILE" ] || die "missing pubkey file: $PUBKEY_FILE"
  PUBKEY="$(tr -d '\r' < "$PUBKEY_FILE")"
fi
[ -n "$PUBKEY" ] || die "--pubkey-file or --pubkey is required"

log "create deploy user: $DEPLOY_USER"
if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
  useradd -m -s /bin/bash "$DEPLOY_USER"
fi
usermod -aG docker "$DEPLOY_USER"

log "install SSH authorized_key"
home_dir="$(getent passwd "$DEPLOY_USER" | cut -d: -f6)"
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$home_dir/.ssh"
auth_keys="$home_dir/.ssh/authorized_keys"
touch "$auth_keys"
chmod 600 "$auth_keys"
chown "$DEPLOY_USER:$DEPLOY_USER" "$auth_keys"
if ! grep -qF "$PUBKEY" "$auth_keys" 2>/dev/null; then
  printf '%s\n' "$PUBKEY" >> "$auth_keys"
fi

log "prepare $DEPLOY_DIR"
mkdir -p "$DEPLOY_DIR"
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$DEPLOY_DIR"

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q 'Status: active'; then
  ufw allow OpenSSH
fi

cat <<EOF

GitHub Actions deploy user ready.

Repository secrets (Settings → Secrets and variables → Actions):
  DEPLOY_HOST     $(curl -fsS https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
  DEPLOY_USER     ${DEPLOY_USER}
  DEPLOY_SSH_KEY  <private key matching the pubkey you passed>

Repository variables (optional):
  DEPLOY_DOMAIN   semantaix.flexsentlabs.com
  DEPLOY_SERVICES api bot_gateway user_gateway nginx

Create environment `production` in GitHub if you want required reviewers before deploy;
then move secrets there and add environment: production to deploy.yml.

First deploy: merge to main after CI passes, or run workflow "Deploy" manually (workflow_dispatch).

EOF
