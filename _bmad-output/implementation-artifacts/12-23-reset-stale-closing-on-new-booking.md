# Story 12.23: Restart the funnel when a returning customer sends a fresh booking intent in `closing`

Status: review

## Story

As a **returning customer whose previous booking was already handed to a human**,
I want a **new** booking message ("хочу забронировать багги завтра в 14:00") to **start a fresh booking**,
so that **I'm not stuck getting "Передам коллегам для подтверждения, на связи." forever instead of being helped**.

**Problem (observed live, багги, Артур, 31 May 2026, 12:01):**

```
Customer: хочу забронировать багги завтра в 14:00
Bot:      Передам коллегам для подтверждения, на связи.
Bot:      Передам коллегам для подтверждения, на связи.   ← (the duplicate is Story 12.24)
```

The customer expected the 12.22 flow (tomorrow IS busy → offer the nearest free slot). Instead the chat was parked in terminal `closing` from an earlier test conversation, and `_dispatch` (`services/api/app/sales/sales_persona_answerer.py`) routes **any** message in `closing` straight to `_handle_closing` — which is unconditionally sticky: it ignores the message text and always re-emits `CLOSING_HANDOFF_LINE` (`"Передам коллегам для подтверждения, на связи."`) while staying in `closing`. So the funnel (`_complete_booking` → `_propose_alternative_or_handoff`, i.e. the just-shipped 12.22 fix) never ran.

**Root cause:** there is no reset/expiry out of a terminal stage. `STAGE_DORMANT` is checked at dispatch but **never set anywhere**; the T+24h follow-up preserves the stage; no idle/TTL demotion exists in `state_repository.py`. A chat that reaches `closing` answers the sticky handoff line to every future message — including a brand-new booking request.

## Acceptance Criteria

1. **Fresh booking intent in `closing` restarts the funnel.** When `current_stage == STAGE_CLOSING` and the incoming message `is_sales_intent(...)`, dispatch routes to `_handle_greeting` (→ `STAGE_SCOPING`) instead of `_handle_closing`. The customer gets the greeting/first-scoping turn, not `CLOSING_HANDOFF_LINE`.
2. **Stale offered slot is cleared.** The greeting restart persists with a clean `Intent` and `last_proposal = None` (reused behaviour: `_handle_greeting` → `_persist` omits `last_proposal`, and `StateRepository.upsert` writes `last_proposal_json = NULL`), so a later pitching turn cannot resurrect the previous conversation's offer.
3. **Non-sales reply in `closing` is unchanged.** A non-booking reply ("спасибо", "ок") in `closing` still routes to `_handle_closing` → sticky `CLOSING_HANDOFF_LINE`, escalate, stays in `closing` (the human still owns it). The greeting LLM is NOT called on this path.
4. **Mid-funnel stages are untouched.** `scoping`/`awaiting_time`/`pitching`/`proposing` keep their existing handlers; the counter-offer / acceptance behaviour from Stories 12.19 and 12.21 must not regress. Only terminal `closing` restarts. (Decision: reset only the terminal/handed-off stage, not a time-gate on mid-funnel.)
5. **Gates green.** `ruff` clean; full suite at 100% coverage on `services/`; new tests for the restart and the sticky-preserved branches.

## Tasks / Subtasks

- [x] **Add the closing-restart branch** (AC 1,3) — in `_dispatch` (`services/api/app/sales/sales_persona_answerer.py`), before the generic `closing` routing, mirror the existing `STAGE_DORMANT` branch: `if current_stage == STAGE_CLOSING: if is_sales_intent(...): return await self._handle_greeting(...); return await self._handle_closing(...)`.
- [x] **Confirm `last_proposal` is cleared** (AC 2) — verified `_persist` defaults `last_proposal=None` and `upsert` writes `last_proposal_json = excluded.last_proposal_json` (unconditional, not COALESCE), so the greeting write nulls it. No extra code needed.
- [x] **Tests** (AC 5) — new `tests/test_sales_persona_answerer_closing_restart.py`: closing + "хочу забронировать багги завтра в 14:00" → greeting/scoping (fresh `Intent`, `last_proposal` None, not the handoff line); closing + "спасибо" → sticky `CLOSING_HANDOFF_LINE`, no LLM call, stays in closing.

## Dev Notes

- **Files:** `services/api/app/sales/sales_persona_answerer.py` (`_dispatch` `closing` branch only). No schema/data-file changes.
- **Reuse, don't reinvent:** `is_sales_intent` (`services/api/app/sales/intent_loader`), `_handle_greeting` (starts a clean `Intent`, persists `→ scoping`), `_handle_closing` (sticky), `_persist`/`StateRepository.upsert` (already nulls `last_proposal`). Mirrors the existing-but-never-triggered `STAGE_DORMANT` branch in `_dispatch`.
- **Why only `closing`:** `proposing`/`pitching` are active negotiation stages whose new-date replies are counter-offers handled by 12.19/12.21 — resetting them on "booking intent" would regress those fixes. `closing` is the one terminal/handed-off stage with no legitimate "resume" semantics.
- **Conventions:** answerers dispatch never raise; immutable `Intent`; time injected via `self._clock()`; `from __future__ import annotations`; ruff E/F/I line-100; 100% coverage gate.

### Project Structure Notes

- Change is localized to one method (`_dispatch`) + its new test file. The duplicate-send half of the live report is a separate concern (Story 12.24 — idempotent inbound delivery).

### References

- [Source: services/api/app/sales/sales_persona_answerer.py#_dispatch / _handle_closing / _handle_greeting]
- [Source: services/api/app/sales/state_repository.py#upsert (last_proposal nulling)]
- Precedent / pattern mirrored: the `STAGE_DORMANT` dispatch branch (sales answerer); live-bug loop guards in `_bmad-output/implementation-artifacts/12-19-recognize-slot-confirmation-and-stop-pitching-reask-loop.md`.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–5).** `_dispatch` now restarts the funnel from `_handle_greeting` when a chat in terminal `STAGE_CLOSING` receives a fresh `is_sales_intent` message; a non-sales reply keeps the sticky `_handle_closing` handoff. The greeting restart reuses `_persist` (no `last_proposal`) so `upsert` nulls the stale offered slot — confirmed by an explicit assertion. Mid-funnel stages and the 12.19/12.21 counter-offer/acceptance handling are untouched.
- **TDD:** `test_closing_with_new_booking_intent_restarts_funnel` was written first and watched fail (got `CLOSING_HANDOFF_LINE` instead of the scoping question), then the dispatch branch made it green; `test_closing_with_non_sales_reply_stays_sticky` guards the preserved sticky path.
- `ruff` clean; full suite **3039 passed at 100% coverage** (CI parity: `pytest --cov --cov-config=.coveragerc`).

### File List

- `services/api/app/sales/sales_persona_answerer.py` (modified — `_dispatch`: closing-stage fresh-intent restart branch)
- `tests/test_sales_persona_answerer_closing_restart.py` (new)
- `_bmad-output/implementation-artifacts/12-23-reset-stale-closing-on-new-booking.md` (new — this story)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified — add `12-23-reset-stale-closing-on-new-booking: review`)
