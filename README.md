# Semantaix Bootstrap

[![CI](https://github.com/flexsent-labs/semantaix/actions/workflows/ci.yml/badge.svg)](https://github.com/flexsent-labs/semantaix/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/flexsent-labs/semantaix/main/.badges/coverage-summary.json)](https://github.com/flexsent-labs/semantaix/actions/workflows/ci.yml)

<!-- LOC:START -->![App source](https://img.shields.io/badge/app%20source-42378%20lines-blue)<!-- LOC:END -->

Docker-first Russian-first Telegram customer-conversation platform (epics 1–16 shipped).
See [`docs/index.md`](docs/index.md) for the as-built architecture map.

## Services

- `api`: FastAPI backend — core business logic for all epics
- `web_ui`: FastAPI admin shell
- `bot_gateway`: Telegram webhook ingress + operator slash commands
- `user_gateway`: per-operator Telegram **user** account (Telethon MTProto) customer channel
- `ingest_worker`: worker heartbeat service
- `scheduler`: scheduler heartbeat service
- `nginx`: reverse proxy (`/api`, `/admin`, `/telegram/webhook`)
- `qdrant`: vector store (provisioned; retrieval is lemma-overlap, not yet vector)
- `postgres`: optional profile service (`--profile with-postgres`; not on the runtime path)

## Quick Start

1. Copy env template:
   - `cp .env.example .env`
2. Build and run:
   - `docker compose up --build -d`
3. Verify health:
   - `curl http://localhost/health/live`
   - `curl http://localhost/api/health/live`
   - `curl http://localhost/admin/health/live`

## DigitalOcean (production VPS)

Target: **https://semantaix.flexsentlabs.com** (DNS on Cloudflare).

```bash
# 1) Server prep on the droplet
sudo bash scripts/digitalocean_bootstrap.sh prepare \
  --domain semantaix.flexsentlabs.com \
  --email you@flexsentlabs.com \
  --cloudflare

# 2) Cloudflare: A record semantaix → droplet IP, DNS only (grey cloud) until step 4

# 3) Set GitHub Actions secrets (see below) — do not put production secrets on the server by hand

# 4) Deploy + TLS + Telegram webhook
sudo bash scripts/digitalocean_bootstrap.sh finish \
  --domain semantaix.flexsentlabs.com \
  --email you@flexsentlabs.com \
  --cloudflare
```

After `finish`: Cloudflare **SSL/TLS → Full (strict)**, then **Proxied** (orange cloud).

Production uses `docker-compose.prod.yml` (compose nginx on `127.0.0.1:8080`; host nginx
terminates TLS). Day-2 updates: `sudo bash /opt/semantaix/scripts/digitalocean_deploy.sh`.

Set production secrets in **GitHub Actions** (repository secrets). Each deploy renders
`/opt/semantaix/.env` from `.env.production` + those secrets via
`scripts/render_production_env.py`. Bot: **@semantaix_bot**. Calendar OAuth redirect:
`https://semantaix.flexsentlabs.com/api/calendar/oauth/callback`.

### GitHub Actions deploy (CI → droplet)

After one-time bootstrap on the droplet, wire **Deploy** workflow (`.github/workflows/deploy.yml`):

1. Generate a deploy key (keep private key for GitHub; install pubkey on the server):

   ```bash
   ssh-keygen -t ed25519 -f ./gha-deploy -N "" -C "semantaix-gha-deploy"
   ```

2. On the droplet (root), after `/opt/semantaix` exists:

   ```bash
   sudo bash /opt/semantaix/scripts/digitalocean_setup_github_actions.sh \
     --pubkey-file ./gha-deploy.pub
   ```

3. GitHub → **Settings → Secrets and variables → Actions** → repository secrets:

   | Secret | Purpose |
   |--------|---------|
   | `DEPLOY_HOST` | Droplet IPv4 |
   | `DEPLOY_USER` | `semantaix-deploy` |
   | `DEPLOY_SSH_KEY` | Deploy SSH private key |
   | `TELEGRAM_BOT_TOKEN` | Production bot token |
   | `OPENROUTER_API_KEY` | LLM API key |
   | `TELEGRAM_ALERT_CHAT_ID` | Operator alert chat |
   | `TELEGRAM_API_ID` | my.telegram.org app id (`user_gateway`) |
   | `TELEGRAM_API_HASH` | my.telegram.org app hash |
   | `INTERNAL_SERVICE_TOKEN` | Service-to-service auth (generate once: `openssl rand -hex 32`) |
   | `CALENDAR_TOKEN_ENCRYPTION_KEY` | Optional — Fernet key for calendar OAuth tokens |
   | `GOOGLE_OAUTH_CLIENT_ID` | Optional — Google Calendar OAuth |
   | `GOOGLE_OAUTH_CLIENT_SECRET` | Optional — Google Calendar OAuth |

4. **Variables** (non-secret): `DEPLOY_DOMAIN` = `semantaix.flexsentlabs.com`;
   `DEPLOY_SERVICES` = `api bot_gateway user_gateway web_ui nginx`.

Deploy runs when **CI** passes on `main`, or manually via **Actions → Deploy**. The workflow
rsyncs code (never `.env` or `.data`), re-renders `.env` from GitHub secrets, rebuilds, and
verifies HTTPS.

## Run Tests

- `python3.11 -m venv .venv && source .venv/bin/activate`
- `pip install -r requirements-dev.txt`
- `pytest` — full suite (unit, API contract, story E2E)
- `pytest -m e2e` — story-aligned E2E subset only
- `pytest --cov --cov-config=.coveragerc --cov-report=term-missing` — same coverage gate as CI

See [_bmad-output/implementation-artifacts/e2e-coverage.md](_bmad-output/implementation-artifacts/e2e-coverage.md) for the story ↔ test matrix.

Gate signoffs (matches CI lint + pytest coverage + Epic 01 live demo): `bash scripts/run_all_epic_feature_signoffs.sh`

## Backup / Restore (Epic 07)

- API: `POST /api/backups/run`, `GET /api/backups`, `GET /api/backups/last-successful`,
  `POST /api/backups/{id}/restore` (body `{"confirm_token":"restore-<id>","target_root":"<dir>"}`).
- Web UI: `/admin/backups` shows the latest successful backup's id, completion
  time, archive path, and size.
- Settings: `BACKUP_DB_PATH`, `BACKUP_ARCHIVE_DIR`, `BACKUP_SOURCE_PATHS` (csv).
- Runbook: see `_bmad-output/implementation-artifacts/epic-07-backup-restore-runbook.md`.

## HITL Contact Configuration

- Default env configuration:
  - `HITL_PRIMARY_OPERATOR_USERNAME`
  - `TELEGRAM_ALERT_CHAT_ID`
  - `HITL_CONFIG_ADMIN_USERNAME`
- Runtime bot command (admin-only) to update operator + chat id:
  - `/hitl_config @flexsentlabs 650934815`
- Access control:
  - only the Telegram username in `HITL_CONFIG_ADMIN_USERNAME` can apply this command.
  - current target admin is `@ajdevy`.
