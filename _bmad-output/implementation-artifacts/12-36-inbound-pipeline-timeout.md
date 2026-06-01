# Story 12.36: Bound the inbound answer pipeline so a slow turn escalates, not stalls (D12, P1)

Status: review

## Story

As a **customer**,
I want **every message to get a reply**,
so that **the bot never goes silent on a slow turn.**

**Problem (observed live, багги, 1 June 2026):** after a series of replies the bot went silent on a resend and a following new message (16+ min). Investigation (round-5 D12) found the *proximate* cause was a disk-full stack outage, but a **latent code defect** turns any slow/stalled turn into permanent silence:

`/conversations/inbound` claims the `trace_id` (Story 12.24) BEFORE running the answer pipeline, then `await`s the pipeline with **no overall timeout** — only per-LLM-call httpx timeouts (30s each, sequential, unbounded count). The grounded-RAG path alone makes ≥2 sequential LLM calls, so a slow turn can exceed the bot_gateway's `inbound_forward_timeout_seconds=45s`. The gateway then times out and **retries**; the retry hits the durable claim and is **deduplicated** (`inbound_idempotent_replay`) — so the customer gets a reply only if the original (still-running) request eventually completes. If it doesn't, the result is permanent silence.

**Root cause** (`services/api/app/main.py` `/conversations/inbound`): the pipeline `await` (`pipeline_task` at the deferred-interim step) has no `asyncio.wait_for` budget; `claim_inbound` exonerated (it correctly dedups by trace_id; a manual resend has a new update_id and is NOT deduped — confirmed in the D12 investigation).

## Acceptance Criteria

1. The answer pipeline for one inbound turn is bounded by `inbound_pipeline_timeout_seconds` (default 40s, **< 45s** forward timeout). On timeout, the in-flight pipeline task is cancelled and the turn **escalates to a human** (ack + HITL ticket) via the existing post-claim error path — never stalls past the gateway deadline.
2. The escalation on timeout is indistinguishable to the customer from any other escalation (gets the ack; operator gets the ticket). The finalized trace records the timeout (`timed_out`/`pipeline_timeout`).
3. A normal (fast) turn is unchanged — no added latency, same delivery/escalation behaviour. The deferred interim ack (Story 12.13) still works.
4. Gates green; 100% coverage.

## Out of scope (documented follow-up — D12 remainder)

The two *other* D12 silence vectors are **disk-full-specific** (that root cause is resolved) and idempotency-delicate, so they are a separate, careful story to avoid re-introducing the duplicate-send bug Story 12.24 fixed:
- Post-claim SQLite writes in the success/escalation region run **outside** the try/except → a `disk-full` `OperationalError` there 500s with the claim held.
- `claim_inbound` is never released → a same-trace retry after a true 500 can't recover.
Both need a deliberate "release-claim-iff-nothing-delivered" design; tracked separately.

## Tasks / Subtasks

- [x] `platform_common/settings.py`: `inbound_pipeline_timeout_seconds: float = 40.0`.
- [x] `services/api/app/main.py` inbound: wrap the pipeline await in `asyncio.wait_for(pipeline_task, timeout=remaining)` (remaining = budget − elapsed-so-far, floored), inside the existing post-claim try; tag the except log with `timed_out`.
- [x] Tests: hung pipeline → escalates to HITL (ack + ticket, response_mode human_only); fast pipeline unchanged.

## Dev Notes

- **Why escalate (not just fail):** the existing `except` already does the right thing (ack + ticket + finalized trace) and keeps the claim — so a gateway retry is correctly deduped (the customer already has the ack). The timeout simply routes a slow turn into that path before the 45s deadline.
- **`asyncio.wait_for` cancels** the pipeline task on timeout, freeing the stuck LLM call.
- **Files:** `services/api/app/main.py` (inbound endpoint), `platform_common/settings.py`.

## References

- Investigation: round-5 Finding D12 (vector #1 — missing pipeline timeout; dedup exonerated).
- [Source: services/api/app/main.py#/conversations/inbound] (claim → pipeline await → post-claim try/except).
- Precedent: `12-24-idempotent-inbound-delivery.md` (the claim this protects); `inbound_forward_timeout_seconds`.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–4).** Added `inbound_pipeline_timeout_seconds=40.0`; wrapped the inbound pipeline await in `asyncio.wait_for(pipeline_task, timeout=budget−elapsed)` inside the existing post-claim try, so a slow/hung turn is cancelled and routed into the existing escalation (ack + HITL ticket); the except log + finalized trace tag `timed_out`/`pipeline_timeout`.
- **TDD:** test written first and watched fail (a 2s-hung pipeline delivered instead of escalating), then green — a hung pipeline with a 0.1s budget escalates to HITL with an ack; the 14 other inbound tests (fast paths) unchanged.
- **Idempotency-safe:** the timeout reuses the existing except, which delivers an ack and keeps the claim — so a gateway retry is correctly deduped (no duplicate send). No change to the claim invariant.
- `ruff` clean; full suite **3126 passed at 100% coverage**.
- **Deferred (documented):** the post-claim-500 / claim-release vectors (#2/#3) — disk-full-specific, idempotency-delicate — remain a separate careful story.

### File List

- `services/api/app/main.py` (modified — pipeline timeout)
- `platform_common/settings.py` (modified — `inbound_pipeline_timeout_seconds`)
- `tests/test_api_conversations_inbound.py` (modified — timeout-escalates test)
- `_bmad-output/implementation-artifacts/12-36-inbound-pipeline-timeout.md` (new)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified)
