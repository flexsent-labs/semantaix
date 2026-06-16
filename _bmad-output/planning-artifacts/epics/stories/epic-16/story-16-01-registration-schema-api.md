# Story 16.01 — Registration Requests Schema + API

## Objective

Land the persistence and api seam for operator self-registration: pending requests, approve/reject mutations, and onboarding event audit. No bot changes in this story.

**As a** platform engineer,
**I want** durable registration-request storage and admin-gated api endpoints,
**So that** the bot and web surfaces can create and resolve operator applications without ad-hoc state.

PRD: **FR-16-01**, **FR-16-03**, **FR-16-04**, **FR-16-09**, **FR-16-10**, **FR-16-11**, **NFR-16-02**, **NFR-16-05**.

## Scope

### In Scope

- **`services/api/app/operator_registration.py`** — `OperatorRegistrationRepository`:
  - Co-located in `semantaix_operators.db` (same file as `operators` table) with WAL pragma on init.
  - Table `operator_registration_requests`:
    ```sql
    CREATE TABLE IF NOT EXISTS operator_registration_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        display_name TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        project_id INTEGER,
        created_at TEXT NOT NULL,
        reviewed_at TEXT,
        reviewed_by TEXT,
        rejection_cooldown_until TEXT
    );
  CREATE UNIQUE INDEX IF NOT EXISTS idx_op_reg_pending_username
    ON operator_registration_requests(username) WHERE status = 'pending';
    ```
  - Table `operator_onboarding_events` (FR-16-10).
  - Methods:
    - `create_request(*, username, chat_id, display_name=None) -> RegistrationRequest` — raises `RegistrationPendingConflict` if pending exists; raises `RegistrationCooldownActive` if rejected within 24 h.
    - `list_by_status(status: str) -> list[RegistrationRequest]`
    - `get(request_id: int) -> RegistrationRequest | None`
    - `approve(*, request_id, reviewed_by, project_id, operator_repository) -> Operator` — single transaction: status→approved, insert operator, record onboarding event `approved`.
    - `reject(*, request_id, reviewed_by) -> RegistrationRequest` — sets cooldown `now+24h`, event not required.
    - `record_onboarding_event(operator_id, event_type) -> None`

- **Api endpoints** in `services/api/app/main.py` (or `operator_registration_router.py` wired via `wire_*`):
  - `POST /operators/register-request` — internal Bearer token only. Body: `{username, chat_id, display_name?}`. Returns `{request_id, status: "pending"}`.
  - `GET /operators/register-requests` — admin session (`require_admin_session` or existing admin auth). Query `status=pending`. Returns list.
  - `POST /operators/register-requests/{id}/approve` — admin session. Body optional `{project_id}` defaulting to default project id. Returns operator dict. 409 on username conflict.
  - `POST /operators/register-requests/{id}/reject` — admin session. Returns updated request.

- **Settings**: no new env vars (uses existing `operators_db_path`).

### Out of Scope

- Bot `/register` command (16-02).
- Telegram admin DM (16-04).
- `user_gateway` changes (16-06).

## Implementation Notes

- Reuse `_connect` + repository patterns from `operators.py`.
- `approve()` calls `operator_repository.create()` inside the same SQLite connection/transaction — use `connection.execute("BEGIN")` … `COMMIT` pattern or a single-connection helper.
- Username normalization: strip leading `@`, store with `@` prefix (match `OperatorRepository` convention).
- `reviewed_by` stores admin username string from session principal.
- Default project: `project_repository.ensure_default_project().id`.

## Test Plan

### Unit

- `tests/test_operator_registration_repository.py` — create, duplicate pending conflict, cooldown after reject, approve creates operator + marks approved, approve idempotent 409 paths, list_by_status, onboarding events.

### Contract

- `tests/test_api_operator_registration_contract.py` — internal register 401 without token; admin list 403 for non-admin; approve round-trip; reject sets cooldown field.

## Automated E2E Verification

Deferred to 16-07.

## Manual Verification

1. `curl` internal register-request with token → 200 + request_id.
2. Admin session approve → operator row in DB.
3. Reject → `rejection_cooldown_until` populated.

## Done Criteria

- 100% coverage on `operator_registration.py` + new router wiring.
- `ruff check .` passes.
