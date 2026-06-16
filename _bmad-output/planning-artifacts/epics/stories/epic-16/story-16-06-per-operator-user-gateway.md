# Story 16.06 — Per-Operator `user_gateway` Sessions

## Objective

Extend Epic 15's QR auth to support one Telethon session per operator. The linked account becomes that operator's **customer-facing chat identity** (Story 16-08 wires inbound/outbound on it).

**As a** registered operator,
**I want** to link the Telegram account my clients message by scanning a QR code,
**So that** the platform can chat with my customers on my behalf from that account.

PRD: **FR-16-07**, **FR-16-12**, **NFR-16-04** (inherits NFR-15-03/04).

## Scope

### In Scope

- **`services/user_gateway/app/operator_auth_repo.py`** — `OperatorTelegramAuthRepository`:
  - Table `operator_telegram_auth` in `semantaix_user_gateway.db`:
    ```sql
    CREATE TABLE IF NOT EXISTS operator_telegram_auth (
        operator_id INTEGER PRIMARY KEY,
        phase TEXT NOT NULL DEFAULT 'idle',
        session_path TEXT NOT NULL,
        started_at REAL,
        updated_at REAL NOT NULL
    );
    ```
  - Session files: `{settings.operator_sessions_dir}/{operator_id}.session` (new setting default `.data/operator_sessions`).

- **Extend `app/routers/auth.py`** (Epic 15.02):
  - `POST /auth/qr_start?operator_id=<int>` — when `operator_id` omitted, use legacy singleton path (Epic 15 backward compat).
  - Per-operator: validate operator exists via api `GET /operators/{id}` (internal token).
  - In-memory state keyed by `operator_id` (dict of `_AuthState` under lock) instead of single global.
  - `GET /auth/status?operator_id=` — returns phase for that operator.
  - `POST /auth/verify_2fa?operator_id=` — scoped 2FA.

- **`services/bot_gateway/app/operator_telegram_link.py`**:
  - Shared module extracted from Epic 15 `/user_login` orchestration.
  - `async def start_operator_telegram_link(*, operator_id, operator_chat_id, user_gateway_client, send_dm)`:
    1. `POST /auth/qr_start?operator_id=`
    2. Send QR as document (not photo).
    3. Poll status 3s × 30.
    4. Handle 2FA relay (operator's next DM text → verify_2fa).
  - On success: api onboarding event `telegram_link_connected`; trigger `OperatorClientPool.start(operator_id)` hook (no-op stub until 16-08, or full start if 16-08 merged).
  - Success DM: `"✓ Аккаунт привязан. Клиенты могут писать вам в личные сообщения — ответы будут приходить с этого аккаунта."`

- `/user_login` command updated to require registered operator and pass `operator_id` from registry lookup.

- **Settings**:
  - `operator_sessions_dir: str = ".data/operator_sessions"`
  - `.env.example` entry.

- **Startup**: `clear_stale_on_startup()` per operator row in `qr_pending`/`2fa_pending`.

### Out of Scope

- Inbound/outbound customer messaging on linked account (Story 16-08).
- `/user_logout` per operator.

## Implementation Notes

- Reuse Epic 15.02 `_await_qr_scan`, `qrcode.make`, document send pattern verbatim — parameterize by `operator_id`.
- Never log `session_path` (NFR-16-04).
- Tests: `MemorySession` only; mock `qr_login.wait()`.
- api internal endpoint to record `telegram_link_connected` onboarding event (or reuse generic onboarding event POST from 16-05).

## Test Plan

### Unit

- `tests/user_gateway/test_operator_auth_repo.py` — per-operator phase CRUD.
- `tests/user_gateway/test_operator_qr_start.py` — qr_start with operator_id; legacy path without id still works.
- `tests/test_bot_gateway_operator_telegram_link.py` — full mock flow.

### Integration

- `tests/user_gateway/test_operator_auth_integration.py` — happy path + 2FA per operator_id.

## Automated E2E Verification

Part of `tests/e2e/test_e2e_epic16_operator_telegram_link.py` in 16-07 (mock Telethon).

## Manual Verification

1. Approved operator taps "Привязать Telegram-аккаунт" → QR document.
2. Scan → success DM; `.data/operator_sessions/<id>.session` exists.
3. Second operator can link independently.

## Done Criteria

- 100% coverage on new/changed `user_gateway` + bot link module.
- Epic 15.02 tests still pass (backward compat without operator_id).
