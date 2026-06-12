# Story 14.03: Message-volume instrumentation (bot_gateway)

Status: done

## Story

As an admin,
I want every customer and operator message tracked with direction and role in `semantaix_usage.db`,
so that the dashboard and `/usage` command can show message volume per project alongside LLM spend.

## Acceptance Criteria

1. `UsageMessageRepository.record(row: UsageMessageRow) -> None` is implemented in `services/api/app/usage/repositories.py` (replacing the 14.01 skeleton):
   - Validates `row.direction ∈ {'in', 'out'}` and `row.participant_role ∈ {'customer', 'operator'}`; raises `ValueError` on mismatch BEFORE touching the DB
   - INSERTs one row into `usage_messages`; the `id` column is auto-assigned by SQLite

2. `services/bot_gateway/app/main.py` gains a `usage_recorder` module-level singleton:
   - Constructed from `UsageRecorder(llm_repo=UsageLlmCallRepository(db_path=settings.usage_db_path), message_repo=UsageMessageRepository(db_path=settings.usage_db_path), hitl_repo=UsageHitlEventRepository(db_path=settings.usage_db_path), queue_maxsize=settings.usage_queue_maxsize)` (same three-repo pattern as the api service)
   - `bootstrap_usage_db(settings.usage_db_path)` is called at module initialisation (before the singleton) so the schema exists before the recorder can enqueue
   - A `@app.on_event("startup")` hook calls `usage_recorder.start()`
   - A `@app.on_event("shutdown")` hook calls `await usage_recorder.aclose()`

3. Inbound **customer** message recording (bot_gateway):
   - One `usage_messages` row with `direction="in"`, `participant_role="customer"` is enqueued per validated customer webhook message
   - Enqueued via `asyncio.create_task(usage_recorder.record(...))` in the customer message branch BEFORE `background_tasks.add_task(_forward_live_and_clear_pending, ...)` so the record fires even if the forward later fails
   - `project_id` resolved by calling `_project_repository.ensure_default_project().id` inside a `try/except Exception` — a lookup failure silently skips recording (no log, no error surface to the customer)
   - `trace_id` from the `trace_id` variable already in scope at that point in the handler
   - `created_at` is UTC ISO-8601 with Z suffix (`datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")`)

4. Inbound **operator** reply recording (bot_gateway):
   - One `usage_messages` row with `direction="in"`, `participant_role="operator"` is enqueued at the entry of `_handle_operator_reply` when a ticket id is successfully resolved (after the ticket-id lookup, BEFORE the `api_client.deliver_operator_reply` call)
   - `project_id` resolved the same way as AC 3 (default project fallback)
   - `trace_id` is `None` (operator replies are not part of a customer trace)

5. Outbound **customer** answer recording (api service):
   - One `usage_messages` row with `direction="out"`, `participant_role="customer"` is enqueued immediately after each `_safe_send_message` call in the two customer-answer delivery branches of `/conversations/inbound`:
     a. `pipeline_result.handled` success branch (line ~2567)
     b. `_dispatch_sales_escalation` branch (before or after the existing `_safe_send_message` call at line ~2229)
   - Uses the existing `usage_recorder` singleton wired in Story 14-02
   - Enqueued via `asyncio.create_task(usage_recorder.record(...))` — non-blocking
   - `project_id` from `ctx.project_id`; skipped if `ctx.project_id is None`
   - `trace_id` from the in-scope `trace_id` variable
   - `created_at` UTC ISO-8601 with Z suffix

6. 100% line coverage on:
   - The new `UsageMessageRepository.record()` body
   - The `bootstrap_usage_db` call and `usage_recorder` startup/shutdown wiring in bot_gateway
   - The inbound customer + operator recording helpers in bot_gateway
   - The outbound recording additions in api/main.py
7. `ruff check .` clean; no new E501 violations (100-char line length)

## Tasks / Subtasks

- [x] Task 1 — Implement `UsageMessageRepository.record` (AC: 1)
  - [x] 1.1 Replace `raise NotImplementedError` with an INSERT SQL in `repositories.py`
  - [x] 1.2 Add `DIRECTIONS = frozenset({'in', 'out'})` and `PARTICIPANT_ROLES = frozenset({'customer', 'operator'})` module-level constants (or inline the validation)
  - [x] 1.3 Validate `direction` and `participant_role` before the INSERT; raise `ValueError` on mismatch

- [x] Task 2 — Wire `UsageRecorder` into bot_gateway (AC: 2)
  - [x] 2.1 Import `bootstrap_usage_db`, `UsageRecorder`, and the three repo classes from `services.api.app.usage.*` in `bot_gateway/app/main.py`
  - [x] 2.2 Call `bootstrap_usage_db(settings.usage_db_path)` at module level (after settings line, before singleton)
  - [x] 2.3 Create `usage_recorder` singleton at module level with all three repos
  - [x] 2.4 Add `@app.on_event("startup")` hook `_start_usage_recorder_on_startup` that calls `usage_recorder.start()`
  - [x] 2.5 Add `@app.on_event("shutdown")` hook `_stop_usage_recorder_on_shutdown` that calls `await usage_recorder.aclose()`

- [x] Task 3 — Instrument inbound customer messages in bot_gateway (AC: 3)
  - [x] 3.1 Identify the exact line in the customer message branch of the webhook handler where `trace_id` is set and the message is confirmed as a customer message (not operator, not dedup-rejected)
  - [x] 3.2 Add a `_enqueue_inbound_customer_message(*, trace_id: str) -> None` helper that resolves `project_id` from the default project and enqueues the record — wrapped so exceptions are swallowed
  - [x] 3.3 Call `asyncio.create_task(_enqueue_inbound_customer_message(trace_id=trace_id))` before `background_tasks.add_task(...)`

- [x] Task 4 — Instrument inbound operator reply in bot_gateway (AC: 4)
  - [x] 4.1 Add a `_enqueue_inbound_operator_message() -> None` helper with the same project lookup + swallowed-exception pattern
  - [x] 4.2 Call it at the top of `_handle_operator_reply`, after `ticket_id` is resolved (only when `ticket_id is not None`) — before the `api_client.deliver_operator_reply` call

- [x] Task 5 — Instrument outbound customer answer in api/main.py (AC: 5)
  - [x] 5.1 Add a `_enqueue_outbound_customer_message(*, project_id: int, trace_id: str | None) -> None` helper in api/main.py that calls `asyncio.create_task(usage_recorder.record(...))`; skips silently if `project_id is None`
  - [x] 5.2 Call `_enqueue_outbound_customer_message(project_id=ctx.project_id, trace_id=trace_id)` immediately after the `_safe_send_message` call in the `pipeline_result.handled` success branch (line ~2567)
  - [x] 5.3 Call `_enqueue_outbound_customer_message(project_id=ctx.project_id, trace_id=trace_id)` immediately after the `_safe_send_message` call in `_dispatch_sales_escalation` (line ~2229) — `project_id: int | None = None` added to `_dispatch_sales_escalation` signature; call site passes `ctx.project_id`

- [x] Task 6 — Tests (AC: 6, 7)
  - [x] 6.1 `tests/test_usage_message_repository.py` — INSERT happy path, direction validation error, participant_role validation error
  - [x] 6.2 `tests/test_bot_gateway_message_usage_wiring.py` — source-inspection + execution tests verifying `usage_recorder` exists, startup/shutdown hooks exist and execute correctly
  - [x] 6.3 `tests/test_bot_gateway_inbound_customer_message_usage.py` — correct payloads for customer and operator inbound messages; error-swallowing for project lookup and recorder failures
  - [x] 6.4 Update `tests/e2e/test_e2e_epic14_llm_round_trip.py` — additionally assert that after the round-trip a `usage_messages` row with `direction="out"` and `participant_role="customer"` exists in the temp SQLite DB

## Dev Notes

### Repository convention

All DB access lives in `*Repository` classes; no raw SQL outside them. `UsageMessageRepository.record()` follows the same `sqlite3.connect(self._db_path) as conn` + `conn.execute(INSERT ...)` pattern as `UsageLlmCallRepository.record()` in the same file. Do NOT use WAL pragma here — `bootstrap_usage_db` already sets WAL on first run.

### `UsageMessageRow` dataclass (existing, from 14-01)

```python
@dataclass(frozen=True)
class UsageMessageRow:
    id: int            # 0 sentinel for new rows (SQLite auto-assigns)
    project_id: int
    direction: str          # 'in' | 'out'
    participant_role: str   # 'customer' | 'operator'
    trace_id: str | None
    created_at: str         # UTC ISO-8601 with Z suffix
```

The `id` field is `0` when constructing a new row; SQLite assigns the real id on INSERT. The INSERT must NOT include `id` in the column list (AUTOINCREMENT).

### `UsageRecorder.record()` payload for messages

```python
await usage_recorder.record(
    tracker_type="messages",
    project_id=project_id,
    payload={
        "direction": "in",              # or "out"
        "participant_role": "customer", # or "operator"
        "trace_id": trace_id,           # str | None
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    },
    trace_id=trace_id,
)
```

The recorder's `_consumer_loop` dispatches `tracker_type="messages"` to `UsageMessageRepository.record()` via `asyncio.to_thread`. The `payload` dict is how the recorder builds the `UsageMessageRow` — the keys match the dataclass fields (see `recorder.py` `_record_message_row` method).

### Cross-service import in bot_gateway

bot_gateway imports from `services.api.app.usage.*` — this is intentional (shared DB schema, shared recorder class). The pattern is established in the repo already (bot_gateway imports `ApiClient`, `OpenRouterClient`, etc. from the api layer). Python path configuration handles this via the monorepo root.

### bootstrap_usage_db idempotency

`bootstrap_usage_db` uses `CREATE TABLE IF NOT EXISTS` throughout — safe to call on every process start. Call it at bot_gateway module level (before the singleton construction) to guarantee the schema exists before the first `record()` call.

### `_project_repository.ensure_default_project()` 

This call is synchronous (SQLite read + optional INSERT). It is safe to call from an async context without `asyncio.to_thread` because the call is fast (single row lookup or insert) and the rest of bot_gateway makes sync SQLite calls without threading too. Wrap in `try/except Exception` so a missing `projects.db` at test time doesn't surface to customers.

### `asyncio.create_task` vs `asyncio.ensure_future`

Use `asyncio.create_task(coro)` — not `asyncio.ensure_future`. The webhook handler already runs inside an event loop, so `create_task` is safe. The task is fire-and-forget; do not `await` it. The recorder's internal queue is bounded (drop-oldest on overflow) so this cannot back-pressure the request path.

### `_dispatch_sales_escalation` — getting `project_id`

`_dispatch_sales_escalation` is a module-level async function in api/main.py. It receives `request` (an `InboundMessageRequest`). The `ctx` (AnswerContext) is NOT passed to it. Instead, add `project_id: int | None` and `trace_id: str` as keyword params to `_dispatch_sales_escalation`'s signature and pass them from the caller (the inbound handler where `ctx.project_id` is in scope). This avoids globals and keeps the helper self-contained.

### TestClient event loop (E2E test)

The E2E test fixture patches `api_main.usage_recorder` BEFORE entering `with TestClient(api_app)`. The FastAPI startup hook calls `usage_recorder.start()` on the TestClient's event loop. After `with TestClient` exits, the shutdown hook drains the queue. Asserting the DB after exiting the `with` block is safe because the queue has been fully drained by then.

For the bot_gateway `usage_recorder`, the same pattern applies if a future test drives through bot_gateway: patch `bot_main.usage_recorder` before `TestClient(bot_app)`.

### What NOT to instrument in this story

- **Interim ack messages** (`минуточку` / "Один момент"): these are implementation-detail messages, not customer-answer messages. Do NOT record them.
- **Rate-limit reply** (`_RATE_LIMIT_REPLY`): not a customer answer. Do NOT record.
- **Pipeline-error ack** (the HITL ack sent when the pipeline crashes): borderline, but excluded for scope — Story 14-04 (HITL event instrumentation) will cover escalation-related sends.
- **Operator DMs** (HITL ticket notifications to operators): excluded for scope.
- **`_send_dm` bot_gateway calls**: these are operator-targeted system DMs, not customer messages. Excluded from this story.

### `DIRECTIONS` and `PARTICIPANT_ROLES` constants

Add these as module-level frozensets in `repositories.py`, adjacent to `CALL_OUTCOMES`:

```python
DIRECTIONS: frozenset[str] = frozenset({'in', 'out'})
PARTICIPANT_ROLES: frozenset[str] = frozenset({'customer', 'operator'})
```

### 100% coverage on bot_gateway startup/shutdown hooks

The startup/shutdown hooks are async functions decorated with `@app.on_event`. The existing `test_bot_gateway_message_usage_wiring.py` (Task 6.2) should use source inspection (check the function exists as a module attribute OR read the source) rather than trying to run the FastAPI lifecycle — the lifecycle requires a running Telegram bot token. Follow the same pattern as `tests/test_usage_recorder_lifespan.py` from Story 14-02.

### File list for this story

**Modified:**
- `services/api/app/usage/repositories.py` — implement `UsageMessageRepository.record()`
- `services/api/app/main.py` — add `_enqueue_outbound_customer_message` helper + two call sites; extend `_dispatch_sales_escalation` signature with `project_id` + `trace_id`
- `tests/e2e/test_e2e_epic14_llm_round_trip.py` — add `usage_messages` assertion

**Created:**
- `services/bot_gateway/app/main.py` — add imports, `bootstrap_usage_db` call, `usage_recorder` singleton, startup/shutdown hooks, inbound recording helpers (add to existing file)
- `tests/test_usage_message_repository.py`
- `tests/test_bot_gateway_message_usage_wiring.py`
- `tests/test_bot_gateway_inbound_customer_message_usage.py`

## Dev Agent Record

### Debug Log

- **Ruff I001 in bot_gateway/main.py**: First import block attempt placed `usage.*` imports before `sales.*`; ruff requires alphabetical order within the `services.api.app.*` group. Fixed by reordering: `hitl → openrouter_client → projects → russian_text → sales.* → telegram_bot_sender → usage.*`.
- **test_message_repo_record_not_implemented**: The 14.01 skeleton test expected `record()` to raise `NotImplementedError`. Implementing it in Task 1 broke this test. Renamed to `test_message_repo_record_is_implemented`; body now calls `repo.record(row)` without expecting an exception.
- **Coverage gaps — bot_gateway startup/shutdown hooks (lines 2287, 2292)**: Source-inspection tests (asserting the function body *contains* certain code) do not execute the function body. Added two async execution tests to `test_bot_gateway_message_usage_wiring.py` that patch `usage_recorder` and call the hooks directly.
- **Coverage gaps — operator message error paths (lines 2094-2095, 2107-2108)**: Added two async tests to `test_bot_gateway_inbound_customer_message_usage.py` mirroring the existing customer-message error-path tests.
- **Coverage gap — api/main.py line 1944**: The `if project_id is None: return` early exit in `_enqueue_outbound_customer_message` was never hit (E2E always patches project_id to 14). Added `tests/test_api_outbound_customer_message_usage.py` with a `project_id=None` call.
- **Ruff I001 in test_usage_message_repository.py**: `DIRECTIONS, PARTICIPANT_ROLES` import order was reversed. Fixed with `ruff check --fix`.

### Completion Notes

- **Task 1**: Added `DIRECTIONS` and `PARTICIPANT_ROLES` frozensets adjacent to `CALL_OUTCOMES` in `repositories.py`. Implemented `UsageMessageRepository.record()` with direction/role validation before INSERT; raises `ValueError` on invalid values without touching the DB.
- **Task 2**: Bot_gateway now imports from `services.api.app.usage.*`, calls `bootstrap_usage_db` at module level, constructs `usage_recorder` singleton with all three repos, and registers startup/shutdown hooks.
- **Task 3**: `_enqueue_inbound_customer_message` helper fire-and-forgets a `direction="in"`, `participant_role="customer"` record; called via `asyncio.create_task` before `pending_forward_outbox.mark_pending`.
- **Task 4**: `_enqueue_inbound_operator_message` helper fire-and-forgets a `direction="in"`, `participant_role="operator"` record; called via `asyncio.create_task` at the top of `_handle_operator_reply`.
- **Task 5**: `_enqueue_outbound_customer_message` in api/main.py skips if `project_id is None`, otherwise fires `asyncio.create_task(usage_recorder.record(...))`. Called after `_safe_send_message` in both the pipeline success branch and `_dispatch_sales_escalation`. The latter's signature was extended with `project_id: int | None = None`; call site passes `ctx.project_id`.
- **Task 6**: 8 repository unit tests, 11 bot_gateway wiring + usage tests, 2 api outbound tests, E2E assertion for outbound row. 100% coverage; `ruff check .` clean.

### File List

**Modified:**
- `services/api/app/usage/repositories.py`
- `services/api/app/main.py`
- `services/bot_gateway/app/main.py`
- `tests/e2e/test_e2e_epic14_llm_round_trip.py`
- `tests/test_usage_repositories_skeleton.py`

**Created:**
- `_bmad-output/implementation-artifacts/14-03-message-volume-instrumentation.md` (this file)
- `tests/test_usage_message_repository.py`
- `tests/test_bot_gateway_message_usage_wiring.py`
- `tests/test_bot_gateway_inbound_customer_message_usage.py`
- `tests/test_api_outbound_customer_message_usage.py`

### Senior Developer Review (AI)

**Outcome:** Approve
**Date:** 2026-06-12

**Layers run:** Inline (blind hunt + edge case hunt + acceptance audit) — specialized subagent types unavailable.

**Action Items:**

- [x] [Review][Defer] `asyncio.Queue` replaced at `UsageRecorder.start()` — any items queued between import and startup hook are discarded [`services/api/app/usage/recorder.py:63`] — deferred, pre-existing (Story 14-02 design)
- [x] [Review][Defer] `bootstrap_usage_db` crash risk if `.data/` directory missing at process start [`services/bot_gateway/app/main.py` module level] — deferred, pre-existing (identical to api service, Docker Compose handles dir creation)
- [x] [Review][Defer] Sync `ensure_default_project()` SQLite call inside async helpers without `asyncio.to_thread` [`services/bot_gateway/app/main.py:2093,2097`] — deferred, pre-existing bot_gateway pattern throughout codebase

### Change Log

- 2026-06-12: Story 14.03 implementation — `UsageMessageRepository.record()`, bot_gateway `UsageRecorder` wiring, inbound customer + operator recording, outbound customer recording in api/main.py, 100% test coverage (AI Agent)
- 2026-06-12: Code review passed — 0 patch findings, 3 deferred (all pre-existing), 3 dismissed (AI Agent)
