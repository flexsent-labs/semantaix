# Story 15.02 — QR Authentication Flow

## Objective

Enable the platform operator to authenticate the Telegram user account by scanning a QR code sent by the existing bot, with 2FA fallback if the account has two-factor authentication enabled. Includes durable auth phase tracking in SQLite so that a `user_gateway` restart during the auth window produces a clean operator-facing error rather than a silent hang.

**As a** platform operator,
**I want** to authenticate the Telegram user account by typing `/user_login` in the bot, scanning the QR sent back to me, and optionally entering my 2FA password,
**So that** `user_gateway` holds a valid persistent Telegram session before any customer message routing begins.

PRD reference: **FR-15-02** (qr_start), **FR-15-03** (status), **FR-15-04** (verify_2fa), **FR-15-05** (QR refresh), **FR-15-06** (session persistence), **FR-15-07** (bot_gateway /user_login), **NFR-15-03** (password never persisted), **NFR-15-04** (session path never logged).

## Scope

### In Scope

#### `services/user_gateway/`

- **`app/auth_session_repo.py`** — `AuthSessionRepository`:
  - SQLite file at `settings.user_gateway_db_path`; WAL mode.
  - Schema (singleton row):
    ```sql
    CREATE TABLE IF NOT EXISTS auth_session (
        id         INTEGER PRIMARY KEY CHECK (id = 1),
        phase      TEXT NOT NULL DEFAULT 'idle',
        started_at REAL,
        updated_at REAL NOT NULL
    );
    ```
  - `get_phase() -> str` — SELECT; returns `'idle'` if no row.
  - `set_phase(phase: str) -> None` — INSERT OR REPLACE with `updated_at=time.time()`.
  - `clear_stale_on_startup() -> str` — UPDATE phase='idle' WHERE phase IN ('qr_pending','2fa_pending'); returns the old phase value for logging (returns `'idle'` if no stale row found).

- **`app/auth_state.py`** — module-level in-memory singleton (cannot be serialized):
  ```python
  @dataclass
  class _AuthState:
      phase: str = "idle"
      client: Optional[TelegramClient] = None
      qr_login: Optional[object] = None  # Telethon QRLogin
      _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

  _state = _AuthState()

  def get_state() -> _AuthState: ...
  ```

- **`app/routers/auth.py`** — FastAPI router, prefix `/auth`:
  - `POST /auth/qr_start`:
    - If phase not in `('idle', 'qr_pending')`: return 409 `{"detail": "already_authenticated"}`.
    - Create `TelegramClient(SQLiteSession(settings.tg_user_session_path), api_id, api_hash)`.
    - Set `client.flood_sleep_threshold = 60` (NFR-15-02).
    - Call `qr_login = await client.qr_login()`.
    - Render QR PNG from `qr_login.url` via `qrcode.make(...)` → base64.
    - Set `_state.phase = 'qr_pending'`, `_state.client = client`, `_state.qr_login = qr_login`.
    - Call `repo.set_phase('qr_pending')`.
    - Fire background task: `asyncio.create_task(_await_qr_scan())`.
    - Return `{"qr_image_b64": "...", "expires_in": 30}`.
  - `GET /auth/status`:
    - Return `{"phase": _state.phase, "authenticated": _state.phase == "authenticated"}`.
  - `POST /auth/verify_2fa`:
    - Body: `{"password": "..."}` (password field, never logged).
    - Acquire `_state._lock`.
    - If `_state.phase != "2fa_pending"` or `_state.client is None`: raise 409 `{"detail": "no_pending_auth: phase is not 2fa_pending; call /user_login to restart"}`.
    - `await _state.client.sign_in(password=body.password)`.
    - On `PasswordHashInvalidError`: return 401 `{"detail": "invalid_password"}`.
    - On success: `_state.phase = 'authenticated'`, `repo.set_phase('authenticated')`.
    - Return `{"status": "authenticated"}`.
    - Password is never assigned to a variable that persists, logged, or stored anywhere.

- **`_await_qr_scan()` background task** (module-level coroutine in `auth.py`):
  ```
  try:
      await qr_login.wait(timeout=30)
      # Scanned, no 2FA
      _state.phase = 'authenticated'
      repo.set_phase('authenticated')
  except asyncio.TimeoutError:
      # Refresh QR
      await qr_login.recreate()
      # Signal bot_gateway to re-send (via /auth/status polling)
      _state.qr_login = qr_login  # updated login object
      # Set phase back to qr_pending (it already is)
  except SessionPasswordNeededError:
      # 2FA required — persist phase BEFORE any await
      _state.phase = '2fa_pending'
      repo.set_phase('2fa_pending')  # durable: survives restart
  ```

- **Startup lifespan hook** in `app/main.py`:
  - On startup: `stale = repo.clear_stale_on_startup()`.
  - If stale in `('qr_pending', '2fa_pending')`: log WARNING `"auth_restart: cleared stale phase=%s; operator must /user_login again"`.
  - Set `_state.phase = 'idle'`, `_state.client = None`, `_state.qr_login = None` on startup.

- **Session path redaction**: follow `_redact_token` pattern from `bot_gateway`. Never pass `settings.tg_user_session_path` to any log call.

#### `services/bot_gateway/`

- **`/user_login` command** added to `app/main.py` (operator-only, same check as other operator commands):
  1. `resp = await user_gateway_client.post("/auth/qr_start")` — if 409 (already authenticated): DM operator "Already authenticated. To re-authenticate, use /user_logout first."
  2. Extract `qr_image_b64` from response. Decode to bytes.
  3. `await bot.send_document(operator_chat_id, document=qr_bytes, filename="login_qr.png", caption="Scan this QR code in Telegram to log in. Valid for 30 seconds.")` — send as **document** not photo, to prevent Telegram compression that can corrupt QR readability.
  4. Poll `GET /auth/status` every 3 seconds for up to 90 seconds:
     - phase == `'authenticated'`: DM operator "✓ User account authenticated successfully." Break.
     - phase == `'2fa_pending'`: DM operator "Two-factor authentication required. Please reply with your 2FA password." Wait for operator reply (next operator message in conversation treated as 2FA password). `POST /auth/verify_2fa {"password": "<operator reply text>"}`. On 401: DM "Incorrect 2FA password. Please try again." Loop. On 200: DM "✓ Authenticated." Break.
     - phase == `'qr_pending'` and QR expired (>30s on qr_pending): `GET /auth/status` still returns qr_pending → call `POST /auth/qr_start` again, send fresh QR with caption "QR expired — here is a new one."
  5. If 90s elapsed with no auth: DM operator "Login timed out. Run /user_login to try again."

- **`UserGatewayClient`** — new thin HTTP client in `services/bot_gateway/app/user_gateway_client.py` (mirrors `ApiClient` pattern): base URL from `settings.user_gateway_base_url` (new Settings field, default `"http://user_gateway:8005"`), auth via `internal_service_token`.

#### Settings

- `platform_common/settings.py`: `user_gateway_base_url: str = "http://user_gateway:8005"`.
- `.env.example`: `USER_GATEWAY_BASE_URL=http://user_gateway:8005`.

### Out of Scope

- Message NewMessage handler and routing (15.03).
- Spam filters (15.04).
- `/user_logout` command — deferred; operator can re-auth by deleting session file and restarting.

## Implementation Notes

- **2FA state durability**: The `SessionPasswordNeededError` is caught in `_await_qr_scan()`. The critical ordering is: write `repo.set_phase('2fa_pending')` **before** any `await` that could be preempted. This ensures the SQLite row is committed even if the task is cancelled immediately after.
- **Restart recovery**: On `user_gateway` boot, `clear_stale_on_startup()` wipes `qr_pending` and `2fa_pending` from SQLite. The in-memory `_state` is always fresh on boot (no Telethon client). If bot_gateway polls `/auth/status` and gets `phase='idle'` after a restart, it will eventually time out its login flow and DM the operator to run `/user_login` again. This is correct behavior — the QRLogin object is gone, no resumption is possible.
- **QR as document**: Telegram compresses photos; a high-density QR code sent as a photo can become unreadable. Sending as `send_document` preserves original bytes.
- **Password never logged**: In `verify_2fa`, the password is received in the request body, immediately passed to `client.sign_in(password=...)`, and no reference to it is retained. Add a linter note (comment) reminding future authors not to add `logger.debug(body)` here.
- **`flood_sleep_threshold`**: set to 60 immediately after client creation, before any API call (NFR-15-02).
- **`TELEGRAM_API_ID` and `TELEGRAM_API_HASH`**: already in `.env.example` from prior work. `user_gateway` reads them from `Settings` (`telegram_api_id: int`, `telegram_api_hash: str` — verify they exist or add them).
- **Test isolation**: Use Telethon's `MemorySession` in all tests. Never create a real MTProto connection in tests. Mock `qr_login.wait()` to simulate success, timeout, and `SessionPasswordNeededError`.

## Test Plan

### Unit

- `tests/user_gateway/test_auth_session_repo.py`:
  - `get_phase()` returns `'idle'` when table empty.
  - `set_phase('qr_pending')` then `get_phase()` returns `'qr_pending'`.
  - `clear_stale_on_startup()` with `qr_pending` → returns `'qr_pending'`, `get_phase()` == `'idle'`.
  - `clear_stale_on_startup()` with `2fa_pending` → returns `'2fa_pending'`, `get_phase()` == `'idle'`.
  - `clear_stale_on_startup()` with `idle` → returns `'idle'`, no change.
  - `clear_stale_on_startup()` with `authenticated` → returns `'authenticated'`, no change (authenticated not cleared).

- `tests/user_gateway/test_auth_state.py`:
  - Default state: `phase='idle'`, `client=None`, `qr_login=None`.
  - Lock is an asyncio.Lock.

- `tests/user_gateway/test_auth_restart.py` (covers the Party Mode fix):
  - **T1** `test_startup_clears_qr_pending`: repo set to `qr_pending`, lifespan startup hook clears it.
  - **T2** `test_startup_clears_2fa_pending`: repo set to `2fa_pending`, lifespan startup hook clears it.
  - **T3** `test_verify_2fa_idle_phase_returns_409`: `_state.phase = 'idle'` → `POST /auth/verify_2fa` → 409, detail starts with `'no_pending_auth'`.
  - **T4** `test_verify_2fa_post_restart_returns_409`: `_state.phase = '2fa_pending'`, `_state.client = None` → `POST /auth/verify_2fa` → 409.
  - **T5** `test_verify_2fa_wrong_password_returns_401`: mock `client.sign_in` to raise `PasswordHashInvalidError` → 401.
  - **T6** `test_verify_2fa_success`: mock `client.sign_in` success → 200 `{"status": "authenticated"}`, `_state.phase == 'authenticated'`.

- `tests/user_gateway/test_auth_router.py`:
  - `POST /auth/qr_start` when idle → 200 with `qr_image_b64` and `expires_in`.
  - `POST /auth/qr_start` when authenticated → 409 `already_authenticated`.
  - `GET /auth/status` returns current phase.
  - `_await_qr_scan` with `wait()` success → phase becomes `authenticated`.
  - `_await_qr_scan` with `SessionPasswordNeededError` → phase becomes `2fa_pending`, repo updated.
  - `_await_qr_scan` with TimeoutError → `qr_login.recreate()` called, phase stays `qr_pending`.

### Contract

- `GET /auth/status` response shape: `{"phase": str, "authenticated": bool}` — assert in tests.
- `POST /auth/verify_2fa` 409 body: `{"detail": str}` where detail starts with `"no_pending_auth"`.

### Integration

- `tests/user_gateway/test_auth_integration.py`:
  - Full happy path: qr_start → mock QR scan → status == authenticated. Uses `MemorySession` Telethon mock.
  - 2FA path: qr_start → mock SessionPasswordNeededError → status shows 2fa_pending → verify_2fa with correct mock password → authenticated.

## Automated E2E Verification

- Full QR flow tested via mock Telethon in integration tests. No real Telegram connection in CI.

## Manual Verification

1. `/user_login` in Telegram bot → receive QR as document.
2. Scan QR in Telegram mobile → bot DMs "✓ User account authenticated successfully."
3. Kill and restart `user_gateway` container while mid-2FA → bot eventually DMs operator to run `/user_login` again (no silent hang).
4. `/user_login` on already-authenticated account → "Already authenticated."
