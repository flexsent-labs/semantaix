# Story 12.22: Defer HITL escalation until the customer accepts the offered alternative slot

Status: review

## Story

As a **customer who asked to book a slot that turns out to be busy**,
I want the bot to **offer me the nearest free alternative and wait for my answer**,
so that **a human is not pulled in before I've even chosen whether to take the offered time, walk away, or counter-offer**.

**Problem (observed live, багги, 31 May 2026 10:19):**

```
Customer: хочу забронировать багги завтра в 14:00
Bot:      Проверяю, минуточку… 🙂
Bot:      К сожалению, это время уже занято. Ближайшее свободное время — 1 июня, 08:00.
Bot:      Спасибо! Передам детали коллегам — подтвердят и вернутся с предложением.   ← BUG
```

Customer reaction: *"почему-то бот сам решил что-то передавать коллегам, хоть я и не ответил ничего"* (the bot decided to pass to colleagues even though I didn't reply anything).

**Root cause** (`services/api/app/sales/sales_persona_answerer.py`, `_propose_alternative_or_handoff` `:1256`): when scoping completes and the requested time is busy, the method returns `response_mode=RESPONSE_MODE_SALES_ESCALATION` with `escalate=True` and `hitl_reason='sales_scoping_complete'` regardless of whether a nearest free alternative was offered. `services/api/app/main.py:2441-2454` routes that into `_dispatch_sales_escalation`, which (a) sends the alternative-slot text, (b) creates a HITL ticket, (c) DMs the operator with the booking summary — all on the same turn, before the customer has agreed to anything.

Story 12.19 (PR #99) already added `last_proposal` persistence so `_handle_pitching` (`:920`) can interpret the customer's *next-turn* acceptance / counter-offer / closure correctly. But it left the *first-turn* escalation in place, so an operator gets pulled in on every busy booking even when the customer might still abandon, counter-offer, or simply not reply.

## Acceptance Criteria

1. **Alternative offered = no first-turn escalation.** When `_propose_alternative_or_handoff` runs with `alternative is not None`, the returned `AnswerResult` has `response_mode is None`, no `escalate` / `hitl_reason` / `escalation_context` metadata; the customer-facing offer text is unchanged. The stage is still persisted as `STAGE_PITCHING` with `last_proposal={"alternative_iso": alternative.isoformat()}` (Story 12.19 contract preserved).
2. **No alternative still escalates.** When `alternative is None` (calendar can't propose anything in the window — service rule with no working hours, lookahead exhausted, etc.), the method keeps the existing behaviour: `response_mode=RESPONSE_MODE_SALES_ESCALATION`, `escalate=True`, `hitl_reason='sales_scoping_complete'`, `escalation_context` from `_format_intent_summary(intent)`. The customer has nothing to accept onto, so a human picks up.
3. **Customer acceptance on the next turn escalates exactly once.** Given the bot offered an alternative on turn N (no ticket), an acceptance on turn N+1 ("да", "ок", "давайте", "давайте на 31-ое в 8") routes through `_handle_pitching` → `_confirm_slot`, which already escalates with `PITCHING_ACCEPT_CONFIRM_LINE` + `hitl_reason='sales_scoping_complete'`. Across both turns the customer sees exactly one escalation, fired only after they accepted.
4. **Recursive busy stays non-escalating.** When the customer's turn-N+1 reply is a parseable counter-offer that is itself busy, `_handle_pitching` re-enters `_complete_booking` → `_propose_alternative_or_handoff` with a fresh `alternative`; the new offer also does not escalate. `last_proposal` is overwritten with the new offered slot. The bot can keep offering without spamming tickets.
5. **Closure / non-acceptance safety net unchanged.** Any non-acceptance, non-counter-offer reply ("всё, спасибо", filler) still routes to `_handoff_after_pitching_followup` (Story 12.19) which escalates with `SCOPING_COMPLETE_HANDOFF_LINE` + `hitl_reason='sales_scoping_complete'` and moves to `STAGE_CLOSING`. Customer-abandoned conversations still reach a human.
6. **Dispatch fallback composes textually, no escalation.** `dispatch_fallback=True` (the tour-preview media moment failed earlier in the turn) still appends `MATERIAL_DISPATCH_FALLBACK_LINE` to the offer text but does not flip the escalation back on — the media failure is orthogonal to whether the customer has accepted.
7. **Gates green.** `ruff` clean; full suite at 100% coverage on `platform_common/` + `services/`.

## Tasks / Subtasks

- [x] **Branch the `AnswerResult` in `_propose_alternative_or_handoff`** (AC 1,2,6) — compute `offered = alternative is not None`, build the shared text + metadata, then return without `response_mode` / escalation keys when `offered`; preserve the escalating return when `not offered`. The `_persist(STAGE_PITCHING, last_proposal=…)` call is shared.
- [x] **TDD test file** (AC 1–6) — new `tests/test_sales_persona_answerer_propose_alternative_defers_hitl.py` covering: direct call with alternative (AC 1), direct call with `alternative=None` (AC 2), two-turn offered → "да" → one escalation (AC 3), two-turn offered → counter-offer busy again → still no escalation (AC 4), two-turn offered → "всё, спасибо" → handoff escalation (AC 5), dispatch_fallback + alternative → no escalation (AC 6).
- [x] **Update existing tests for the new contract** (AC 1,2) — `tests/test_sales_persona_answerer_pitching.py::test_requested_time_busy_offers_alternative` drops `response_mode == 'sales_escalation'` and adds the no-escalation assertions; `test_requested_time_busy_no_alternative_hands_off` is strengthened to lock down `response_mode == 'sales_escalation'`, `hitl_reason`, and `escalation_context`. `tests/test_sales_persona_answerer_awaiting_time.py::test_awaiting_time_reply_with_time_busy_offers_alternative` adds the no-escalation assertion so the awaiting_time → busy+alt path is also pinned.

## Dev Notes

- **Files:** `services/api/app/sales/sales_persona_answerer.py` (`_propose_alternative_or_handoff` `:1256`). No new constants, no data-file changes, no schema changes. `_handoff_after_scoping` (`:1324`) is intentionally untouched — its `free=True` (slot is free) and "calendar can't verify" branches still escalate, which is correct: there is no offer awaiting customer acceptance there.
- **Reuse, don't reinvent:** `_handle_pitching` (`:920`, Story 12.19) — counter-offer / acceptance / closure handling, including the recursive `_complete_booking` → `_propose_alternative_or_handoff` entry. `_confirm_slot` (`:963`) escalates with `PITCHING_ACCEPT_CONFIRM_LINE`. `_handoff_after_pitching_followup` (`:1016`) escalates with `SCOPING_COMPLETE_HANDOFF_LINE`. `_format_intent_summary` (`escalation_context`) is unchanged — it now only fires from the acceptance / closure escalation sites.
- **Log line semantics:** the `sales_answerer_handled` log entry keeps `sales_turn_kind=scoping_complete_busy_alternative`; the `hitl_reason` field is suppressed on the non-escalating branch so log scrapers can't confuse it with a ticket-creating turn.
- **Conventions:** answerer dispatch never raises; immutable `Intent` (`replace`); time injected; `from __future__ import annotations`; ruff E/F/I line-100; 100% coverage gate.

### Project Structure Notes

- All changes are localized to the sales answerer + its tests. `last_proposal` already round-trips through `state_repository.upsert`/`get`. No new data layer, no migrations, no settings changes.

### References

- [Source: services/api/app/sales/sales_persona_answerer.py#_propose_alternative_or_handoff `:1256`]
- [Source: services/api/app/sales/sales_persona_answerer.py#_handle_pitching `:920`] · [Source: services/api/app/sales/sales_persona_answerer.py#_confirm_slot `:963`] · [Source: services/api/app/sales/sales_persona_answerer.py#_handoff_after_pitching_followup `:1016`]
- [Source: services/api/app/main.py#_dispatch_sales_escalation `:2159`]
- Precedent / pattern mirrored: [`12-19-recognize-slot-confirmation-and-stop-pitching-reask-loop.md`](12-19-recognize-slot-confirmation-and-stop-pitching-reask-loop.md) (introduced `last_proposal`; this story closes the loop on first-turn escalation) and [`12-21-proposing-counter-offer-before-acceptance.md`](12-21-proposing-counter-offer-before-acceptance.md) (sibling fix for `_handle_proposing`).

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–7).** `_propose_alternative_or_handoff` now branches the returned `AnswerResult` on `alternative is not None`: with an alternative the result is non-escalating (no `response_mode`, no `escalate` / `hitl_reason` / `escalation_context`), so `_dispatch_sales_escalation` is not invoked — but the stage transition to `STAGE_PITCHING` and the `last_proposal={"alternative_iso": …}` persistence are kept exactly as Story 12.19 expects. With no alternative the existing escalation path is preserved verbatim.
- **No second-turn changes needed.** `_handle_pitching` (Story 12.19) already escalates on acceptance via `_confirm_slot` and on closure / non-acceptance via `_handoff_after_pitching_followup`; recursive busy turns re-enter `_propose_alternative_or_handoff` and stay non-escalating naturally.
- **TDD:** the new test file's six assertions were written first and watched fail (all 5 alternative-present cases asserted `response_mode is None` against the existing escalating code); after the fix 6/6 are green. Existing tests asserting the old escalation contract were updated in the same commit and explicitly cite Story 12.22 in their inline comments.
- `ruff check .` clean; full suite **3037 passed at 100% coverage** (CI parity: `pytest --cov --cov-config=.coveragerc`).

### File List

- `services/api/app/sales/sales_persona_answerer.py` (modified — `_propose_alternative_or_handoff` returns a non-escalating `AnswerResult` when `alternative is not None`; escalation path preserved when `alternative is None`)
- `tests/test_sales_persona_answerer_propose_alternative_defers_hitl.py` (new — six tests covering AC 1–6)
- `tests/test_sales_persona_answerer_pitching.py` (modified — `test_requested_time_busy_offers_alternative` updated to the new contract; `test_requested_time_busy_no_alternative_hands_off` strengthened to lock down the still-escalating no-alt path)
- `tests/test_sales_persona_answerer_awaiting_time.py` (modified — `test_awaiting_time_reply_with_time_busy_offers_alternative` pinned to no-escalation)
- `_bmad-output/implementation-artifacts/12-22-defer-hitl-until-alternative-accepted.md` (new — this story)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified — add `12-22-defer-hitl-until-alternative-accepted: review`)
