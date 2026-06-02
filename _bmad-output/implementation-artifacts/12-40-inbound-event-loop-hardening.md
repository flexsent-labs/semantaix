# Story 12.40: Inbound handler runs all DB off the event loop (D12, P1)

Status: review

## Story

As a **customer**,
I want **every message to get a reply even when the database is briefly slow/locked**,
so that **the bot doesn't go globally silent after one message and need a restart.**

**Problem (round-6 #35, 2 Jun 2026):** `#34` replied, then `#35 Хочу записаться на багги сегодня в 14:00…` → no reply, 7+ min; bot needed a restart. Pattern across rounds: answers one/few, then silent on everything.

**Root cause (mechanism CONFIRMED; trigger Hypothesized — see investigation):** the `/conversations/inbound` handler ran **synchronous SQLite I/O directly on the event loop** — the per-message idempotency gates (`find_by_trace_id`, `claim_inbound`) first, then the success/escalation/except paths (`hitl_ticket_repository.create/assign/find_active_for_chat`, `_persist_answer_trace`, runtime-config reads). `asyncio.wait_for` (12.36) can only cancel at an `await`; it **cannot interrupt a blocking sync call**. Under WAL lock contention / disk pressure (data volume was at 90%, ENOSPC observed during this session), a blocking SQLite call wedges the **whole event loop** — and because 12.36's own escalation path also did sync SQLite, even the fallback ack never sent → permanent silence until restart. The per-message gates are the worst: they run first on **every** message, so a contended `answer_traces` DB wedges the loop at message *entry* → the bot can't process *any* message.

## Acceptance Criteria

1. Every DB call in `conversations_inbound` and its escalation helpers (`_escalate_calendar_availability`, `_dispatch_sales_escalation`) runs via `asyncio.to_thread` — a slow/locked SQLite blocks a worker thread, never the event loop.
2. The per-message idempotency gates (`find_by_trace_id`, `claim_inbound`) run off the loop (the global chokepoint).
3. Behaviour unchanged on the happy/escalation/dedup paths (the full inbound + escalation suites stay green).
4. With the loop never wedged, the process stays responsive (health checks answer) and **recovers on its own** once the DB frees up — no restart needed.
5. Gates green; 100% coverage.

## Why this is the right layer

12.36 bounds the async pipeline (a hung LLM escalates). 12.40 closes the *other* half: the synchronous I/O the bound can't cancel. Together: a degraded turn either escalates (async hang, 12.36) or blocks only a worker thread, not the loop (sync I/O, 12.40). The disk pressure that triggers this was also remediated operationally (Docker cache prune, ~8.7 GB).

## Tasks / Subtasks

- [x] Wrap the per-message gates (`find_by_trace_id`, `claim_inbound`) in `asyncio.to_thread`.
- [x] Wrap the main handler's success / except / escalation DB calls (`_persist_answer_trace`, `hitl_ticket_repository.create/assign/find_active_for_chat`, `_effective_inbound_ack_message`, `_pick_assignee_for_chat`, `_effective_hitl_operator_username`).
- [x] Wrap the escalation helpers' DB calls (calendar + sales), including the nested project/operator resolvers (via a lambda where a sync arg is itself a DB read).
- [x] Test (TDD): the idempotency gates run off the event-loop thread (handler awaited directly so the test thread IS the loop thread; assert the gates ran on a different thread).

## Dev Notes

- **`asyncio.to_thread` forwards args/kwargs** to the sync callable, so each call site keeps its exact signature. Where a sync argument is itself a DB read (`_effective_inbound_ack_message(project_id=_resolve_inbound_project_id(...))`), the whole expression is wrapped in a `lambda` so both run on the worker thread.
- **Branch-neutral:** wrapping `f(...)` as `await asyncio.to_thread(f, ...)` adds no branches, so existing inbound/escalation tests preserve coverage; they're the regression net for the mechanical edits.
- **Trigger still wants logs:** the *mechanism* (sync I/O un-cancellable by 12.36) is Confirmed by code; the specific #35 *trigger* (DB lock / disk-full at 08:29) needs the api logs to confirm — see the investigation case file.
- **Files:** `services/api/app/main.py`.

## References

- Investigation: `_bmad-output/implementation-artifacts/investigations/booking-dialog-round6-blockers-investigation.md` — Finding D12.
- Round-6 QA #35 (second message silent).
- Pairs with: `12-36-inbound-pipeline-timeout.md` (async-hang half).
- [Source: services/api/app/main.py#conversations_inbound], [#_escalate_calendar_availability], [#_dispatch_sales_escalation].

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–5, D12).** All synchronous SQLite I/O in the inbound flow now runs via `asyncio.to_thread`. A locked/slow DB blocks a worker thread, not the event loop, so the bot stays responsive and self-recovers instead of going globally silent until a restart.
- **TDD:** the gates-off-loop behaviour test was watched RED (gates ran on the loop thread) then GREEN. Full inbound + escalation suites green for no-regression.
- **Pairs with 12.36** (bounds the async hang) and the disk remediation (removes the likely trigger).

### File List

- `services/api/app/main.py` (modified — inbound handler + escalation helpers)
- `tests/test_api_conversations_inbound.py` (modified — gates-off-loop test)
- `_bmad-output/implementation-artifacts/12-40-inbound-event-loop-hardening.md` (new)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified)
