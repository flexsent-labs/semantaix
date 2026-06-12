# Semantaix Bootstrap

<!-- LOC:START -->![App source](https://img.shields.io/badge/app%20source-36835%20lines-blue)<!-- LOC:END -->

Initial Docker-first skeleton for the Semantaix Option B architecture.

## Services

- `api`: FastAPI backend
- `web_ui`: FastAPI admin shell
- `bot_gateway`: Telegram webhook ingress placeholder
- `ingest_worker`: worker heartbeat service
- `scheduler`: scheduler heartbeat service
- `nginx`: reverse proxy (`/api`, `/admin`, `/telegram/webhook`)
- `qdrant`: vector store
- `postgres`: optional profile service (`--profile with-postgres`)

## Quick Start

1. Copy env template:
   - `cp .env.example .env`
2. Build and run:
   - `docker compose up --build -d`
3. Verify health:
   - `curl http://localhost/health/live`
   - `curl http://localhost/api/health/live`
   - `curl http://localhost/admin/health/live`

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
