# Story 12.24: Idempotent inbound delivery (no duplicate customer sends on a slow turn)

Status: review

## Story

As a **customer messaging the bot**,
I want a **single** reply to my message,
so that **I'm not spammed with the same line twice when the bot's turn happens to be slow**.

**Problem (observed live, багги, Артур, 31 May 2026, 12:01):** the bot sent `"Передам коллегам для подтверждения, на связи."` **twice** to one customer message.

**Stronghold (logs, `trace=tg-update-726742793`, UTC):**

```
api  09:01:04.435 inbound_received           (POST #1)
bg   09:01:14.437 inbound_forward_failed     (bot_gateway 10s timeout — api still sending)
api  09:01:16.459 inbound_received           (POST #2 = retry, SAME trace_id — idempotency MISS)
api  09:01:23.949 sales_escalation_coalesced (POST #1 finishes → SEND #1)
api  09:01:31.054 sales_escalation_coalesced (POST #2 finishes → SEND #2)  ← duplicate
api  09:01:31.483 inbound_idempotent_replay  (POST #3 retry — trace now persisted → no send)
```

**Root cause** (two parts):
1. **Idempotency window open during the turn.** The api dedup gate keys on the `answer_traces` row, which is written only at the **END** of `/conversations/inbound` (`_persist_answer_trace` → `AnswerTraceRepository.write`). A retry that arrives *while the first request is still in-flight* misses `find_by_trace_id` and reprocesses, re-sending the customer line.
2. **Forward timeout shorter than a slow turn.** `ApiClient` forwards `/conversations/inbound` with a 10s timeout (`services/bot_gateway/app/api_client.py`). A sales-escalation turn blocks the response on synchronous Telegram sends (customer line + operator DM) and exceeded 10s (~19s here), so bot_gateway treated the timeout as failure and retried (`_forward_inbound_with_retry`, delays `"2,5,10"`) — firing the duplicate.

The trace_id is derived from the Telegram `update_id`, so every retry of one update shares one key (confirmed: three POSTs all `tg-update-726742793`).

## Acceptance Criteria

1. **Atomic claim closes the in-flight window.** `/conversations/inbound` claims the `trace_id` BEFORE the pipeline. The first caller wins; a concurrent/retried caller with the same `trace_id` is deduplicated (`inbound_idempotent_replay`, `reason=in_flight_claim`) and returns `{"deduplicated": true, "delivered": false, ...}` WITHOUT running the pipeline or sending anything.
2. **Finalized-replay gate unchanged.** A retry arriving AFTER the first finished still returns the cached outcome via the existing `find_by_trace_id` gate (one ack / one ticket / one operator DM — `test_inbound_replaying_same_trace_id...` stays green).
3. **Genuine-outage retry still delivers.** An api-outage forward never returns 200, so it never claims; bot_gateway's existing retry delivers once the api is back (the retry's original purpose — preserved).
4. **Forward timeout exceeds worst-case turn.** The bot_gateway → api inbound forward uses a configurable timeout (`Settings.inbound_forward_timeout_seconds`, default 45s) well above a slow turn, so a slow-but-successful forward isn't mistaken for a failure and retried mid-send. The webhook still 200s to Telegram first, so the long forward timeout never delays Telegram.
5. **Gates green.** `ruff` clean; full suite at 100% coverage on `services/`; new tests for the claim, the in-flight dedup, and the timeout pass-through.

## Tasks / Subtasks

- [x] **Claim table + method** (AC 1) — `services/api/app/answer_trace.py`: add an `inbound_claims (trace_id PRIMARY KEY, claimed_at)` table to `init_schema`, and `claim_inbound(trace_id) -> bool` (`INSERT OR IGNORE`; `cursor.rowcount == 1`). Persists (named-volume / restart parity).
- [x] **Wire the claim** (AC 1,2,3) — `services/api/app/main.py` `/conversations/inbound`: after the existing finalized `find_by_trace_id` gate, `if not answer_trace_repository.claim_inbound(trace_id): return {deduplicated…}` (log `inbound_idempotent_replay`, `reason=in_flight_claim`). The end-of-turn `write` to `answer_traces` is unchanged.
- [x] **Timeout setting + plumbing** (AC 4) — `platform_common/settings.py` `inbound_forward_timeout_seconds: int = 45`; `ApiClient.forward_inbound(..., timeout_seconds=None)` → `_post(timeout_override=...)`; `_forward_inbound_safe` passes `settings.inbound_forward_timeout_seconds`.
- [x] **Tests** (AC 5) — `tests/test_answer_trace_claim.py` (claim wins/loses, requires trace_id, survives reinit); `tests/test_api_conversations_inbound.py::test_inbound_inflight_duplicate_is_deduped_without_processing` (pre-claim → dedup, no pipeline, no send); `tests/test_bot_gateway_api_client.py` (timeout override + default). Updated the e2e journey `_forward` stub to accept `timeout_seconds`.

## Dev Notes

- **Why a separate `inbound_claims` table (not reuse `write`):** `write` is intentionally "insert-or-return-existing" and a test (`test_write_is_idempotent_for_same_trace_id`) locks that contract. Claiming via the same row would either break that test or leave the trace stuck "pending". A dedicated lock table keeps `write`/`answer_traces` semantics untouched.
- **Known edge (optional later hardening):** an api crash AFTER claim but BEFORE the send leaves a pending claim with no answer; a retry dedups to it and the customer gets nothing. Mitigate later with a staleness window on pending claims (reclaim if older than N s). Out of scope for the observed slow-but-successful turn.
- **Files:** `services/api/app/answer_trace.py`, `services/api/app/main.py` (inbound endpoint), `platform_common/settings.py`, `services/bot_gateway/app/api_client.py`, `services/bot_gateway/app/main.py`.
- **Reuse:** the existing `find_by_trace_id` gate and `inbound_idempotent_replay` log event; `_post(timeout_override=...)` (already used by `submit_operator_upload`).
- **Conventions:** SQLite sync repo; `from __future__ import annotations`; ruff E/F/I line-100; 100% coverage gate.

### Project Structure Notes

- Pairs with Story 12.23 (stale-`closing` restart) — together they resolve both halves of the 31 May live report (wrong line + duplicate). Independent PRs off `origin/main`.

### References

- [Source: services/api/app/answer_trace.py#claim_inbound] · [Source: services/api/app/main.py#/conversations/inbound idempotency gate]
- [Source: services/bot_gateway/app/api_client.py#forward_inbound] · [Source: platform_common/settings.py#inbound_forward_timeout_seconds]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–5).** Added an `inbound_claims` lock table + `claim_inbound` to `AnswerTraceRepository`; `/conversations/inbound` now atomically claims the `trace_id` before the pipeline, so a retry arriving mid-flight is deduplicated (`reason=in_flight_claim`) with no pipeline run and no send. The finalized-replay gate and `answer_traces.write` are unchanged. Raised the bot_gateway inbound-forward timeout to a configurable 45s so a slow-but-successful turn isn't retried in the first place.
- **TDD:** repo + endpoint tests written first and watched fail (`AttributeError: claim_inbound`), then implemented to green. The existing sequential triple-POST dedup test stays green (covered by the finalized gate).
- **Regression fixed:** the e2e journey's `_forward` stub gained `timeout_seconds=None` to match the new `forward_inbound` signature.
- `ruff` clean; full suite **3043 passed at 100% coverage** (CI parity: `pytest --cov --cov-config=.coveragerc`).

### File List

- `services/api/app/answer_trace.py` (modified — `inbound_claims` table + `claim_inbound`)
- `services/api/app/main.py` (modified — inbound endpoint claims trace_id before the pipeline)
- `platform_common/settings.py` (modified — `inbound_forward_timeout_seconds: int = 45`)
- `services/bot_gateway/app/api_client.py` (modified — `forward_inbound` timeout pass-through)
- `services/bot_gateway/app/main.py` (modified — `_forward_inbound_safe` passes the timeout)
- `tests/test_answer_trace_claim.py` (new)
- `tests/test_api_conversations_inbound.py` (modified — in-flight dedup test)
- `tests/test_bot_gateway_api_client.py` (modified — timeout override + default tests)
- `tests/e2e/test_e2e_pipeline_journey.py` (modified — `_forward` stub signature)
- `_bmad-output/implementation-artifacts/12-24-idempotent-inbound-delivery.md` (new — this story)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified — add `12-24-idempotent-inbound-delivery: review`)
