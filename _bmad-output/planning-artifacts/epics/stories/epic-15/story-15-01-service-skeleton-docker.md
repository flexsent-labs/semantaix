# Story 15.01 — Service Skeleton + Docker Integration

## Objective

Stand up the `user_gateway` FastAPI service as a deployable container on port 8005 with health endpoints and correct Docker configuration. No Telethon client logic is added here — this story creates the substrate every later Epic 15 story builds on.

**As a** platform engineer,
**I want** a deployable `user_gateway` FastAPI service with health endpoints and proper Docker integration,
**So that** the service infrastructure is in place and the platform can health-check it before any Telegram client logic is wired.

PRD reference: **FR-15-01** (port 8005, Telethon import), **FR-15-12** (docker-compose block), **FR-15-13** (/health/live), **NFR-15-01** (100% coverage), **NFR-15-06** (.env key), **NFR-15-07** (code conventions).

## Scope

### In Scope

- **New service directory** `services/user_gateway/` with:
  - `app/__init__.py`
  - `app/main.py` — creates FastAPI app via `create_service_app("user_gateway", lifespan=lifespan)` (imports lifespan stub that does nothing yet), exports `app`
  - `requirements.txt` — `telethon>=1.36`, `qrcode[pil]>=7.4`, `httpx>=0.27` (plus `fastapi`, `uvicorn[standard]`, `pydantic-settings` from `platform_common`)
  - `Dockerfile` — mirrors `services/bot_gateway/Dockerfile` exactly (Python 3.11-slim, virtualenv, `pip install -r requirements.txt`, `uvicorn app.main:app --host 0.0.0.0 --port 8005`)
- **`docker-compose.yml`** new service block:
  ```yaml
  user_gateway:
    build:
      context: .
      dockerfile: services/user_gateway/Dockerfile
    env_file: .env
    volumes:
      - app_data:/app/.data
    ports:
      - "8005:8005"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8005/health/live')"]
      interval: 15s
      timeout: 3s
      retries: 3
    restart: unless-stopped
    depends_on:
      api:
        condition: service_healthy
  ```
- **`platform_common/settings.py`**: add `user_gateway_db_path: str = ".data/semantaix_user_gateway.db"` and `tg_user_session_path: str = ".data/user_gateway.session"` — the session path setting is needed by Story 15.02; declare it here.
- **`.env.example`**: add `TG_USER_SESSION_PATH=.data/user_gateway.session` under the existing `TELEGRAM_*` block.
- **`nginx/nginx.conf`** (if applicable): no routing needed — `user_gateway` is an internal service not exposed via nginx.
- **`CLAUDE.md` architecture table**: add `| user_gateway | 8005 | Telegram user account MTProto gateway |` to the service table.
- **Health endpoints**: provided for free by `create_service_app` (`/health/live`, `/ready`, `/startup`). No extra code needed.

### Out of Scope

- Telethon client creation, QR login, auth endpoints (15.02).
- Message routing, NewMessage handler (15.03).
- Spam filters (15.04).
- Any Settings fields specific to auth state (15.02 adds `AuthSessionRepository`).

## Implementation Notes

- **`create_service_app` usage**: follows identical pattern to `services/bot_gateway/app/main.py` and `services/scheduler/app/main.py`. Import from `platform_common.app_factory`. Pass `lifespan=lifespan` where lifespan is a no-op `@asynccontextmanager` stub for now.
- **`from __future__ import annotations`** at top of every new module (NFR-15-07).
- **`Dockerfile` pattern**: copy `services/bot_gateway/Dockerfile`, change port to 8005 and context path to `services/user_gateway`. Do not add complexity.
- **Settings convention**: `tg_user_session_path` follows `*_db_path` naming family. Telethon's `SQLiteSession` constructor accepts a file path string directly.
- **`app_data` volume**: already declared in `docker-compose.yml` from earlier epics. The `user_gateway` block is the first consumer of this volume outside `api`. Verify `volumes:` top-level section includes `app_data:` — if not, add it.
- **No nginx change**: `user_gateway` talks only to `api` (internal Docker network). No public route.
- **Logging**: `create_service_app` configures structured JSON logging automatically. No extra setup.

## Test Plan

### Unit

- `tests/user_gateway/test_main.py`:
  - Import `app` from `services/user_gateway/app/main.py` without error.
  - `GET /health/live` → 200 `{"status": "ok"}` (or whatever `create_service_app` returns — match the existing pattern from `tests/test_api_health.py` or `tests/test_bot_gateway_health.py`).
  - `GET /ready` → 200.
  - `GET /startup` → 200.
- `tests/user_gateway/test_settings_user_gateway.py`:
  - `Settings()` with no extra env vars populates `user_gateway_db_path = ".data/semantaix_user_gateway.db"` and `tg_user_session_path = ".data/user_gateway.session"`.

### Contract

- N/A — no auth or routing endpoints yet.

### Integration

- `docker compose build user_gateway` succeeds (CI smoke). Verified by including the build in the CI matrix if it isn't already.

## Automated E2E Verification

None for this story — no observable business behavior. Coverage is unit only.

## Manual Verification

1. `docker compose up --build user_gateway` → `curl http://localhost:8005/health/live` → `{"status": "ok"}`.
2. Confirm `.data/` directory is shared with `api` container (both read the same `app_data` volume).
