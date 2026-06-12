# Story 14.04: HITL ticket lifecycle event instrumentation

Status: done

## Story

As an admin,
I want every HITL ticket lifecycle transition (created, assigned, replied, resolved) tracked with ticket_id, project_id, and trace_id in `semantaix_usage.db`,
so that the dashboard can show HITL volume, resolution latency, and escalation trends per project.

## Acceptance Criteria

1. `HITL_EVENT_TYPES: frozenset[str] = frozenset({"created", "assigned", "replied", "resolved"})` constant is added to `services/api/app/usage/repositories.py` alongside the existing `CALL_OUTCOMES` and `DIRECTIONS`/`PARTICIPANT_ROLES` constants.

2. `UsageHitlEventRepository.record(row: UsageHitlEventRow) -> None` is implemented in `services/api/app/usage/repositories.py` (replacing the 14.01 skeleton):
   - Validates `row.event_type ∈ HITL_EVENT_TYPES`; raises `ValueError` on mismatch BEFORE touching the DB
   - INSERTs one row into `usage_hitl_events`; the `id` column is auto-assigned by SQLite

3. `_enqueue_hitl_event(*, project_id: int | None, event_type: str, ticket_id: int, trace_id: str | None) -> None` helper added to `services/api/app/main.py` (same fire-and-forget pattern as `_enqueue_outbound_customer_message`):
   - Skips silently when `project_id is None`
   - Calls `asyncio.create_task(_record())` where `_record()` swallows all exceptions (NFR-8)

4. HITL lifecycle call sites in `services/api/app/main.py` are instrumented:
   - `conversations_inbound` main escalation path: `created` after `hitl_ticket_repository.create()`, `assigned` after `hitl_ticket_repository.assign()` — uses `ctx.project_id` and in-scope `trace_id`
   - `_escalate_calendar_availability`: same `created` + `assigned` pair — `project_id` param added to function signature, call sites updated
   - `_dispatch_sales_escalation`: same `created` + `assigned` pair — `project_id` already in scope
   - Pipeline error handler path in `conversations_inbound`: same pair — uses `ctx.project_id if ctx is not None else _default_project_id()`
   - `route_hitl_ticket`: `assigned` after `hitl_ticket_repository.assign()` — uses `_default_project_id()`
   - `resolve_hitl_ticket` (converted from `def` to `async def`): `resolved` after `hitl_ticket_repository.resolve()` — uses `_default_project_id()`
   - `deliver_hitl_ticket_reply`: `replied` BEFORE `hitl_ticket_repository.resolve()`, then `resolved` AFTER — both use `_default_project_id()`

5. 100% line coverage on all new code paths.
6. `ruff check .` clean; no new E501 violations.

## Tasks / Subtasks

- [x] Task 1 — Implement `UsageHitlEventRepository.record` (AC: 1, 2)
  - [x] 1.1 Add `HITL_EVENT_TYPES` frozenset constant to `repositories.py`
  - [x] 1.2 Replace `raise NotImplementedError` with validation + INSERT SQL
  - [x] 1.3 Validate `event_type` before INSERT; raise `ValueError` on mismatch

- [x] Task 2 — Add `_enqueue_hitl_event` helper to api/main.py (AC: 3)
  - [x] 2.1 Add helper after `_enqueue_outbound_customer_message`
  - [x] 2.2 Follows same fire-and-forget + exception swallow pattern (NFR-8)

- [x] Task 3 — Instrument all HITL lifecycle call sites (AC: 4)
  - [x] 3.1 `conversations_inbound` main escalation path (created + assigned)
  - [x] 3.2 `_escalate_calendar_availability` (created + assigned; add `project_id` param)
  - [x] 3.3 `_dispatch_sales_escalation` (created + assigned)
  - [x] 3.4 Pipeline error handler path (created + assigned)
  - [x] 3.5 `route_hitl_ticket` (assigned)
  - [x] 3.6 `resolve_hitl_ticket` → convert to `async def` + add resolved event
  - [x] 3.7 `deliver_hitl_ticket_reply` (replied then resolved)

- [x] Task 4 — Tests (AC: 5, 6)
  - [x] 4.1 `tests/test_usage_hitl_event_repository.py` — INSERT for all four event types, invalid type raises ValueError before DB, trace_id nullable, auto-increment id, HITL_EVENT_TYPES constant
  - [x] 4.2 Update `tests/test_usage_repositories_skeleton.py` — `test_hitl_event_repo_record_is_implemented`
  - [x] 4.3 `tests/test_hitl_ticket_lifecycle_instrumentation.py` — unit tests for `_enqueue_hitl_event` (skip on None project_id, create_task fires, error swallowed, payload fields, all four event types) + endpoint-level tests for deliver/route/resolve
  - [x] 4.4 `tests/test_hitl_lifecycle_records_usage.py` — integration test: full inbound→escalate→reply lifecycle produces created→assigned→replied→resolved in DB order
  - [x] 4.5 `tests/e2e/test_e2e_epic14_hitl_round_trip.py` — e2e smoke tests: escalation writes created+assigned; reply writes replied+resolved; NFR-8 (broken recorder does not block HITL)

## Dev Notes

### HITL event type constant

```python
HITL_EVENT_TYPES: frozenset[str] = frozenset({"created", "assigned", "replied", "resolved"})
```

Added to `repositories.py` alongside `CALL_OUTCOMES`, `DIRECTIONS`, `PARTICIPANT_ROLES`.

### `resolve_hitl_ticket` sync→async

`resolve_hitl_ticket` was `def` (synchronous endpoint). Converted to `async def` so `asyncio.create_task` can be called inside it. FastAPI transparently handles both sync and async route handlers.

### `project_id` sourcing at standalone HITL endpoints

The `hitl_tickets` table has no `project_id` column. At inbound call sites, `ctx.project_id` is available. At standalone endpoints (`/route`, `/resolve`, `/reply`), `_default_project_id()` is used as fallback (same pattern as bot_gateway in Story 14.03).

### `_escalate_calendar_availability` signature change

Added `project_id: int | None = None` parameter. All call sites updated to pass `ctx.project_id`. This is backwards-compatible (default None).

### NFR-8: recorder failure swallowing

`_enqueue_hitl_event` wraps the recorder call in:
```python
async def _record() -> None:
    try:
        await usage_recorder.record(...)
    except Exception:
        pass
asyncio.create_task(_record())
```

Recorder errors (closed, queue overflow, DB write fail) are silently discarded. The HITL state machine never sees them.

### `replied` fires BEFORE `resolved`

In `deliver_hitl_ticket_reply`, the order is deliberate:
1. `_enqueue_hitl_event(... "replied" ...)`
2. `hitl_ticket_repository.resolve(...)` — advances DB state
3. `_enqueue_hitl_event(... "resolved" ...)`

This preserves the audit trail: the reply happened, THEN the resolve happened.

## Dev Agent Record

### Completion Notes

- All 4 lifecycle events instrumented across 7 call sites in api/main.py
- 44 new tests; full suite 3664 passed, 100% coverage maintained
- `ruff check .` passes clean
- `resolve_hitl_ticket` converted from `def` to `async def` (required for `asyncio.create_task`)
- Integration + e2e tests confirm fire-and-forget tasks commit via TestClient startup/shutdown lifecycle

## File List

- `services/api/app/usage/repositories.py` — Added `HITL_EVENT_TYPES` constant; implemented `UsageHitlEventRepository.record()`
- `services/api/app/main.py` — Added `_enqueue_hitl_event` helper; instrumented 7 call sites; converted `resolve_hitl_ticket` to async; added `project_id` param to `_escalate_calendar_availability`
- `tests/test_usage_hitl_event_repository.py` — New: repository unit tests
- `tests/test_usage_repositories_skeleton.py` — Updated: `test_hitl_event_repo_record_is_implemented`
- `tests/test_hitl_ticket_lifecycle_instrumentation.py` — New: enqueue helper + endpoint unit tests
- `tests/test_hitl_lifecycle_records_usage.py` — New: integration test (full lifecycle → DB rows)
- `tests/e2e/test_e2e_epic14_hitl_round_trip.py` — New: e2e smoke + NFR-8 test

## Change Log

- 2026-06-12: Story 14.04 implemented and tested.
