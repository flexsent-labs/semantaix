# Story 14.04 — HITL event instrumentation (api)

## Objective
Wire `hitl.py` ticket-lifecycle transitions to record one row in `usage_hitl_events` per state change (`created | assigned | replied | resolved`) via the `UsageRecorder` seam. Counts events, not messages — operator reply auto-resolves per FR-3, so a single reply produces TWO rows (`replied` + `resolved`).

**As an** admin or operator,
**I want** to see HITL ticket-lifecycle event counts per project per day,
**So that** I can spot operator load, escalation rate, and resolution turnaround independent of LLM cost.

PRD reference: **FR-30** (HITL Event Capture).

## Scope

### In Scope
- **`UsageHitlEventRepository.record(*, project_id, event_type, ticket_id, trace_id, created_at)`** implementation in `services/api/app/usage/repositories.py` (replacing the 14.01 skeleton):
  - Validates `event_type ∈ {'created','assigned','replied','resolved'}` at the Python boundary (in addition to the SQL CHECK constraint).
  - INSERTs one row.
- **`hitl.py` instrumentation** at each lifecycle transition (services/api/app/hitl.py):
  - **`created`** — after a new ticket persists in `hitl_tickets` (status `open`), call `recorder.record(tracker_type='hitl', project_id=..., payload={event_type='created', ticket_id=..., created_at=now}, trace_id=...)`.
  - **`assigned`** — after a ticket transitions to `assigned` (operator routing), record `event_type='assigned'`.
  - **`replied`** — when an operator reply is delivered via `/hitl/tickets/{id}/reply`, record `event_type='replied'` BEFORE the auto-resolve step.
  - **`resolved`** — after the auto-resolve step transitions the ticket to `resolved`, record `event_type='resolved'`. A successful operator reply therefore writes TWO rows in sequence.
- **`trace_id` propagation** — `hitl_tickets` row may carry the originating inbound's `trace_id` (depends on existing schema; check before refactoring). For `created`/`assigned`, use the inbound's `trace_id`; for `replied`/`resolved`, use the operator-reply request's `trace_id` (which may differ from the original inbound). Each row carries the `trace_id` of the transition that produced it.
- **No new schema** — Epic 14 reads / observes only; no change to `hitl_tickets` or `hitl_runtime_config` (alert thresholds added in 14.09).
- **Recorder access** — `hitl.py` runs in the api, so it has direct access to the `UsageRecorder` singleton (no `internal_service_token` hop needed). Inject the recorder via the existing api dependency-injection seam.

### Out of Scope
- LLM-call instrumentation (14.02).
- Message-volume instrumentation (14.03).
- Daily roll-up (14.05).
- Dashboard, API endpoints, `/usage` bot command (14.06–14.08).
- Alerting (14.09).
- Any change to `hitl_tickets` schema or HITL lifecycle behavior — Epic 14 is observer-only on this surface.
- Counting individual operator-reply messages (those go through the message tracker via 14.03; HITL tracker counts EVENTS).

## Implementation Notes
- **Single-write per transition** — each transition records ONE row. A reply-then-auto-resolve sequence writes TWO rows (one for `replied`, one for `resolved`).
- **Inject the recorder via the api dep tree** — `hitl.py` likely takes its dependencies via the FastAPI `Depends(...)` pattern; add `UsageRecorder` to the same wiring. Don't reach for a module-level singleton inside `hitl.py`.
- **Fire-and-forget through the in-process recorder** — same async fire-and-forget guarantee as the LLM tracker; HITL ticket transitions never block on the usage write.
- **`ticket_id`** is non-NULL by the schema's NOT NULL constraint — every event has a parent ticket. If a transition is somehow attempted without a ticket id (a bug), the validation fails fast.
- **`created_at`** uses the injected clock's `now`.
- **`project_id`** sourced from the ticket row (`hitl_tickets.project_id`). Epic 10 / 10.5 makes this reliable.
- **Structured logging** — no new log events in this story beyond what `hitl.py` already emits; the `usage_record_failed` event from 14.02 covers all three trackers.
- **Idempotency note** — a single HTTP call to `/hitl/tickets/{id}/reply` writes BOTH `replied` and `resolved` rows. If the request is retried (e.g. by a flaky client), the rows DUPLICATE. The dashboard / `/usage` math doesn't break (counts are still correct *for this fingerprint*), but be aware: HITL events follow at-least-once semantics from the perspective of the api endpoint's retry surface. Accepted because operator-reply duplication is rare and self-evident in the audit log.

## Test Plan

### Unit
- `tests/test_usage_hitl_event_repository.py`:
  - `record(event_type='created', ...)` inserts; round-trip OK.
  - All four valid `event_type` values insert successfully.
  - Invalid `event_type='archived'` raises `ValueError` BEFORE touching the DB.
  - `ticket_id` is required (NOT NULL); writing without it raises.
- `tests/test_hitl_ticket_lifecycle_instrumentation.py`:
  - Creating a ticket via the normal `hitl.py` create path → recorder receives one item with `event_type='created'`, the correct `ticket_id`, the correct `project_id`, the originating `trace_id`.
  - Assigning a ticket → recorder receives `event_type='assigned'`.
  - Operator-reply auto-resolve path → recorder receives TWO items in order: `event_type='replied'`, then `event_type='resolved'`. Each carries the operator-reply request's `trace_id`.
  - Recorder failure during one transition logs `usage_record_failed` but does NOT block the HITL state machine (NFR-8): the ticket still progresses.

### Contract
- N/A — no new api endpoints.

### Integration
- `tests/test_hitl_lifecycle_records_usage.py` — boot the api with a fresh `.data/`; create a ticket via `/conversations/inbound` that escalates; assign + reply via `/hitl/tickets/{id}/reply`; assert `usage_hitl_events` contains rows in order `created → assigned → replied → resolved` with matching `ticket_id`.

## Automated E2E verification
- `tests/e2e/test_e2e_epic14_hitl_round_trip.py` (`@pytest.mark.e2e @pytest.mark.epic("14") @pytest.mark.story("14-04")`):
  - Send a customer message that escalates to HITL → assert `usage_hitl_events` has `created` (and `assigned` if auto-assigned).
  - Send an operator reply via the bot gateway path → assert `replied` + `resolved` events recorded in order.
  - Force the recorder to fail mid-transition → assert the HITL ticket still progresses (the customer gets the operator's reply); `usage_record_failed` log appears.

## Manual Verification
1. `docker compose up --build -d`; trigger a HITL escalation via a Telegram customer message that no answerer handles → `sqlite3 .data/semantaix_usage.db "SELECT * FROM usage_hitl_events ORDER BY id;"` shows the `created` (+ possibly `assigned`) rows.
2. Reply as the operator via Telegram → confirm the `replied` + `resolved` rows appear with the matching `ticket_id`.
3. Force `usage.db` corruption (chmod 000) → confirm the HITL reply still delivers to the customer; `usage_record_failed` log appears.

## Done Criteria
- 100% line coverage on the new repository method + all `hitl.py` instrumentation diffs.
- `ruff check .` passes.
- All four `event_type` values exercise an insert path in tests.
- HITL state machine unchanged from a customer/operator POV — Epic 14 is purely additive.
- Recorder-failure injection does not block HITL transitions (NFR-8 verified by test).
- E2E HITL round-trip green.
