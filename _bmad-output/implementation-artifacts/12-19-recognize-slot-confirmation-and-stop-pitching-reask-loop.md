# Story 12.19: Recognize slot confirmation in PITCHING and stop the re-ask loop

Status: ready-for-dev

## Story

As a **customer who was offered an alternative booking slot**,
I want my confirmation ("давайте на 31-ое в 8", "да", "ок") to be **understood**,
so that **the bot confirms the slot and hands me to a human instead of repeating the same "time busy" message forever**.

**Problem (observed live, багги, 30 May 2026):**

```
Customer: хочу забронировать багги завтра в 14:00
Bot:      К сожалению, это время уже занято. Ближайшее свободное время — 31 мая, 08:00.
Customer: давайте на 31-ое в 8                ← confirms the offered slot
Bot:      К сожалению, это время уже занято. Ближайшее свободное время — 31 мая, 08:00.   ← BUG (verbatim repeat)
```

"Анна Иванова" is the bot's sales persona, not a human. **Root cause** (`services/api/app/sales/sales_persona_answerer.py`): the busy message is emitted by `_propose_alternative_or_handoff` (`:1132`), which persists `current_stage=STAGE_PITCHING` **without** a `last_proposal`. The next turn routes to `_handle_pitching` (`:913`), which **never re-interprets the reply** — it reloads the stale intent (`dates` still = the busy `"завтра в 14:00"`) and calls `_complete_booking(stage_before=STAGE_PITCHING)`, which re-checks the same busy slot → re-emits the identical message. Every non-closure reply in PITCHING loops. The bot already escalated to a human on the first busy turn, yet keeps auto-answering. Contrast `_handle_awaiting_time` (`:882`, re-extracts) and `_handle_proposing` (`:1879`, handles `is_acceptance` + counter-offers) — PITCHING has neither.

## Acceptance Criteria

1. **Offered slot is remembered.** When `_propose_alternative_or_handoff` offers an alternative, the slot is persisted as `last_proposal = {"alternative_iso": <alt.isoformat()>}` (and `None` when busy with no slot, or for the free/handoff path).
2. **Acceptance is recognized and names the slot.** Given PITCHING with an offered slot, when the reply `is_acceptance` (lemma overlap incl. "давайте"/"да"/"ок"/"согласен"; reuses `acceptance.py`) **and** carries no new parseable date, then the bot replies naming the slot — `PITCHING_ACCEPT_CONFIRM_LINE` = `"Отлично, передаю детали коллеге на подтверждение — {day_month} на {time}."` (e.g. "31 мая на 08:00") — escalates (`hitl_reason=sales_scoping_complete`, `escalation_context` carries the booking summary), and moves to `STAGE_CLOSING`.
3. **Counter-offer beats acceptance.** Given a reply that is BOTH an acceptance lemma AND a parseable date (e.g. "давайте на 1 июня" — `parse_russian_date_span` parses "1 июня"), the counter-offer wins: dates are updated via the existing `_merge_dates_from_customer_message` and `_complete_booking(stage_before=STAGE_PITCHING)` re-runs (no concrete time → ask once → `STAGE_AWAITING_TIME`; concrete busy → new alternative; concrete free → confirm). It must NOT confirm the originally-offered slot.
4. **No verbatim loop.** Closure ("всё, спасибо") and any non-acceptance/non-counter-offer reply route to a handoff that speaks `SCOPING_COMPLETE_HANDOFF_LINE` (NEVER the `SLOT_BUSY_LINE`/"Ближайшее свободное время" text), escalates, and moves to `STAGE_CLOSING` (sticky via the existing `_handle_closing`). A second follow-up never re-enters the loop.
5. **Free-handoff path is safe.** When PITCHING was reached via `_handoff_after_scoping` (free/can't-verify; `last_proposal=None`), a bare "да" falls through to the handoff (AC 4) — it must NOT falsely confirm a slot.
6. **Ambiguous accept guard.** An accept word + an unparseable refined time ("давайте в 8") confirms the **offered** slot and ships the verbatim customer text to the operator — never guesses a different time.
7. **Gates green.** `ruff` clean; full suite 100% coverage; new tests for each branch.

## Tasks / Subtasks

- [ ] **Persist the offered slot** (AC 1) — in `_propose_alternative_or_handoff` pass `last_proposal={"alternative_iso": alternative.isoformat()}` (or `None`); set `last_proposal=None` explicitly in `_handoff_after_scoping`.
- [ ] **Constant + `__all__`** (AC 2) — add `PITCHING_ACCEPT_CONFIRM_LINE` near the copy block (`:127`), export it.
- [ ] **Rework `_handle_pitching`** (AC 2,3,4,5,6) — priority order: (a) counter-offer via `_merge_dates_from_customer_message` → if `dates` changed, `_complete_booking(..., stage_before=STAGE_PITCHING, base_metadata={"sales_turn_kind":"pitching_counter_offer"})`; (b) `last_proposal is not None and is_acceptance(...)` → `_confirm_offered_slot`; (c) else (closure/no-op) → `_handoff_after_pitching_followup`.
- [ ] **Helper `_confirm_offered_slot`** (AC 2,6) — names slot from `last_proposal["alternative_iso"]` via `_MONTHS_GENITIVE` + `strftime('%H:%M')`; `sales_turn_kind=pitching_accept_confirmed` (or `pitching_accept_no_slot` when iso absent → `SCOPING_COMPLETE_HANDOFF_LINE`); `→ STAGE_CLOSING`, escalate, `escalation_context=_format_intent_summary(intent)`.
- [ ] **Helper `_handoff_after_pitching_followup`** (AC 4) — speaks `SCOPING_COMPLETE_HANDOFF_LINE`, records `sales_closure_detected=is_no_more(...)`, `sales_turn_kind=pitching_followup`, `→ STAGE_CLOSING`, escalate.
- [ ] **Tests** (AC 7) — new `tests/test_sales_persona_answerer_pitching_reask_guard.py` (mirror fixtures from `tests/test_sales_persona_answerer_pitching.py`): reported "давайте на 31-ое в 8" → confirms "31 мая на 08:00" → closing; bare "да" → confirm; "давайте в 8" (ambiguous) → confirm offered + verbatim; "давайте на 1 июня" → counter-offer → awaiting_time (not mis-accepted); "всё, спасибо" → handoff, no busy line; filler non-accept → handoff (loop killed); free-handoff resume (`last_proposal=None`) + "да" → handoff; direct unit test of `_confirm_offered_slot(alternative_iso=None)`. Extend `tests/test_sales_persona_answerer_pitching.py` to assert the busy path now persists `last_proposal`.

## Dev Notes

- **Files:** `services/api/app/sales/sales_persona_answerer.py` (`_propose_alternative_or_handoff` `:1132`, `_handoff_after_scoping` `:1190`, rework `_handle_pitching` `:913`, new constant + 2 sinks, `__all__`).
- **Reuse, don't reinvent:** `is_acceptance` (`acceptance.py`) — "давайте" already a seed; `_merge_dates_from_customer_message` (`:1925`, uses `parse_russian_date_span`); `is_no_more` (`closure.py`); `_format_intent_summary`; `_MONTHS_GENITIVE`; the sticky `_handle_closing` (`:2234`). Mirrors the `_handle_proposing` acceptance pattern (`:1894`).
- **Why acceptance fixes "давайте на 31-ое в 8" without parsing:** the existing parser returns `None` for "31-ое в 8" (ordinal day + bare hour — see Story 12.20), so the counter-offer branch is a no-op and it's treated as acceptance of the slot the bot just offered (which IS 31 мая 08:00) — exactly right. Story 12.20 makes the bot *also* re-verify restated *different* times autonomously.
- **Conventions:** answerers dispatch never raise; immutable `Intent` (`replace`); time injected; `from __future__ import annotations`; ruff E/F/I line-100; 100% coverage gate.

### Project Structure Notes

- All changes are localized to the sales answerer + its tests. No schema/data-file changes; `last_proposal` already round-trips through `state_repository.upsert`/`get` (used today by `_render_and_persist_proposal`).

### References

- [Source: services/api/app/sales/sales_persona_answerer.py#_handle_pitching / _propose_alternative_or_handoff]
- [Source: services/api/app/sales/acceptance.py#is_acceptance] · [Source: data/russian_sales_acceptance.txt]
- [Source: services/api/app/sales/sales_persona_answerer.py#_handle_proposing (acceptance pattern to mirror)]
- Precedent: `_bmad-output/implementation-artifacts/12-11-graceful-decline-and-loop-guard-in-scoping.md` (live-bug loop guard).

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–7).** `_propose_alternative_or_handoff` now persists the offered slot as `last_proposal={"alternative_iso": …}` (and `None` when no slot); `_handoff_after_scoping` persists `last_proposal=None`. `_handle_pitching` is reworked into three branches: (a) a parseable date in the reply (`_merge_dates_from_customer_message`) is a counter-offer → re-run `_complete_booking`; (b) `is_acceptance` with a remembered `last_proposal` → new `_confirm_slot` names the slot (`PITCHING_ACCEPT_CONFIRM_LINE`) and parks in `closing`; (c) closure / anything else → new `_handoff_after_pitching_followup` (generic completion line, never the busy line) → `closing`. Counter-offer is checked before acceptance so "давайте на 1 июня" is not mis-accepted onto the offered slot.
- **Reported case fixed:** "давайте на 31-ое в 8" is unparseable by the current extractors, so it routes to (b) and confirms the offered 31 мая 08:00 — naming it. (Story 12.20 makes the bot re-verify restated *different* times.)
- **Existing pitching tests repurposed:** the completion-logic tests in `test_sales_persona_answerer_pitching.py` previously drove `_complete_booking` via a bare `"ну что?"` follow-up (the looping path); they now drive it via a realistic counter-offer message, preserving coverage of free / busy+alt / busy-no-alt / ask / calendar-gate branches. Acceptance / closure / no-loop guarantees live in the new `test_sales_persona_answerer_pitching_reask_guard.py`.
- ruff clean; full suite **3030 passed at 100% coverage**.

### Amendments

- **Copy reworded (2026-05-31, operator feedback).** `PITCHING_ACCEPT_CONFIRM_LINE` changed from `"…коллеге — подтвердят {day_month} на {time}."` to `"…коллеге на подтверждение — {day_month} на {time}."` (AC 2 updated above). Rationale: the bot must not promise the colleague *will* confirm (`подтвердят` presumes the outcome) — it only states the booking is passed *for* confirmation. The slot is still named, so AC 2 ("the bot replies naming the slot") holds. The change is to the constant only; the 5 test assertions derive their expected text from `PITCHING_ACCEPT_CONFIRM_LINE.format(...)`, so they tracked the new copy and stayed green (`ruff` + full suite re-run).

### File List

- `services/api/app/sales/sales_persona_answerer.py` (modified — `PITCHING_ACCEPT_CONFIRM_LINE`, reworked `_handle_pitching`, new `_confirm_slot` + `_handoff_after_pitching_followup`, `last_proposal` persisted by `_propose_alternative_or_handoff`/`_handoff_after_scoping`, `__all__`)
- `tests/test_sales_persona_answerer_pitching_reask_guard.py` (new)
- `tests/test_sales_persona_answerer_pitching.py` (modified — completion tests reframed as counter-offers + `last_proposal` assertions)
