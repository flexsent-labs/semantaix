# Story 12.10: Ask the customer for a date/time when a booking has none

Status: ready-for-dev

<!-- Created via bmad-create-story. Calendar-gated per product decision (see AC-4). -->

## Story

As a **prospective customer booking a service**,
I want the bot to **ask me for the desired date and time when I haven't given a concrete one**,
so that **it can check real availability and confirm (or offer an alternative) instead of vaguely punting me to a human**.

**Business value:** Today a booking like *"хочу забронировать багги завтра"* (a date, no time) — or one with no time at all once the other scoping fields are filled — completes scoping and falls straight to the generic *"Передам детали коллегам"* handoff. The calendar-aware completion (commit `0373b5a`) can only check a slot when a concrete time is present, so without a time the customer never gets the free/busy answer the system is otherwise capable of. One clarifying turn turns a dead-end handoff into a real, calendar-checked booking.

## Acceptance Criteria

1. **Ask fires (calendar-actionable, no time).** Given the project has calendar **enabled** and **exactly one active service** (`duration_minutes IS NOT NULL`), and scoping is complete but the collected booking has **no concrete date+time** (`extract_requested_start(intent.dates, …)` returns `None`), when the bot would otherwise hand off, then it instead replies asking for the desired **date and time** and persists the conversation in a new `awaiting_time` stage. No HITL ticket / handoff is emitted on this turn.

2. **Follow-up with a time completes the booking.** Given the conversation is in `awaiting_time`, when the customer replies with a concrete date+time, then the bot re-extracts the intent (merging the reply), runs the existing requested-slot check, and **confirms** (slot free) or **offers the nearest free slot** (slot busy) — identical to the current calendar-aware completion. The slot check is reached on this turn (not a second ask).

3. **Bounded to one ask (no loop).** Given the conversation is in `awaiting_time`, when the customer's reply **still** carries no concrete time, then the bot hands off to a human using the existing completion line (`SCOPING_COMPLETE_HANDOFF_LINE`) — it does **not** ask a second time.

4. **Gated: unchanged when not calendar-actionable.** Given calendar is **disabled**, OR there is **not exactly one** active service, when scoping completes without a time, then behavior is **unchanged** (generic handoff exactly as today). The ask only fires when the requested time would be actionable. *(Product decision: ask only when the calendar can be checked.)*

5. **Combined date+time prompt (not time-only).** The clarifying question asks for **date and time together**, because `intent_merge` **replaces** the `dates` field rather than combining (a bare follow-up "14:00" would otherwise overwrite a previously-collected "завтра"). A self-contained reply keeps the merge correct.

6. **No regressions; gates green.** Existing tests that assert the no-time→handoff behaviour in the **calendar-active** case are updated to the new ask-first behaviour; the calendar-disabled / no-service handoff tests stay as-is. `ruff check .` clean; full suite passes at **100% coverage** on `platform_common/` + `services/`.

## Tasks / Subtasks

- [ ] **Add the `awaiting_time` stage + dispatch route** (AC: 1, 2, 3)
  - [ ] Add `STAGE_AWAITING_TIME = "awaiting_time"` to the stage constants (`sales_persona_answerer.py:69-76`).
  - [ ] Add a dispatch branch in `try_answer` (`~454-497`): `if current_stage == STAGE_AWAITING_TIME → _handle_awaiting_time(...)`.
  - [ ] Consider whether `awaiting_time` should be in `_ASIDE_INTERCEPT_STAGES` (so "сколько стоит?" mid-clarification still routes to the aside handler) — mirror what `scoping`/`pitching` do.
- [ ] **Factor the scoping extract+merge into a reusable helper** (AC: 2)
  - [ ] Extract the LLM-call + `_validate_payload` + `intent_merge` block from `_handle_scoping` (`595-633`) into a private helper returning `(merged_intent, next_question) | skip`.
  - [ ] `_handle_awaiting_time` reuses it to re-extract the customer's date+time reply, then calls `_complete_booking(stage_before=STAGE_AWAITING_TIME, …)`. Do **not** route through `_handle_pitching` — it does NOT re-extract intent (`694`), so the reply would be ignored.
- [ ] **Add the "ask for time" decision + prompt in completion** (AC: 1, 3, 4, 5)
  - [ ] Add `_should_ask_for_time(ctx, intent) -> bool`: true iff calendar enabled + settings present + **exactly one** active service rule + `extract_requested_start(intent.dates, now=ctx.now, project_tz=…)` is `None`. Factor the shared "calendar-actionable" gate so it does not drift from `_check_requested_slot` (`742-800`).
  - [ ] In `_complete_booking` (`703-740`), before the slot check: `if stage_before != STAGE_AWAITING_TIME and await self._should_ask_for_time(...)` → persist `STAGE_AWAITING_TIME` (with current intent) and return an `AnswerResult` whose text is the new `ASK_FOR_TIME_LINE`. When `stage_before == STAGE_AWAITING_TIME`, skip the ask (bound to one) → fall through to slot-check-or-handoff.
  - [ ] Add `ASK_FOR_TIME_LINE` constant, e.g. *"Уточните, пожалуйста, желаемые дату и время — проверю по календарю и подтвержу."*
- [ ] **Tests** (AC: all) — 100% coverage
  - [ ] Update existing calendar-active no-time assertions: `tests/test_sales_persona_answerer_scoping.py` (~150, 174), `tests/test_sales_persona_answerer_pitching.py` (`test_no_dates_plain_handoff` ~251 + the no-time cases ~238/247), `tests/test_sales_material_dispatch_pipeline.py` (~195) — only those whose fixture has calendar enabled + one service.
  - [ ] New: ask fires (calendar-active, no time) → `awaiting_time` + ASK line, no ticket.
  - [ ] New: gated off — calendar disabled and no-single-service each still hand off (AC-4).
  - [ ] New: `awaiting_time` + concrete time → slot check (free→confirm, busy→alternative).
  - [ ] New: `awaiting_time` + still no time → `SCOPING_COMPLETE_HANDOFF_LINE` handoff (no re-ask).
  - [ ] New: dispatch routes `awaiting_time` to `_handle_awaiting_time`.

## Dev Notes

### Files to touch

- **`services/api/app/sales/sales_persona_answerer.py`** *(UPDATE — the booking state machine)*
  - **Current state.** Stage dispatch keys off `state["current_stage"]` (`~454-497`): `new → _handle_greeting`, `scoping → _handle_scoping`, `pitching → _handle_pitching`, plus pricing/proposing/closing/dormant and `_ASIDE_INTERCEPT_STAGES`. `_handle_scoping` (`595-677`) runs one OpenRouter `complete_json`, validates via `_validate_payload`, `intent_merge`s into the stored intent; if **not** `is_complete()` it asks the LLM's `next_question` and stays in `scoping`; if complete it fires the tour-preview media moment and calls `_complete_booking`. `_handle_pitching` (`679-701`) loads the stored intent (**no re-extraction**), detects closure words (`is_no_more`), and re-runs `_complete_booking`. `_complete_booking` (`703-740`) calls `_check_requested_slot`; `UNAVAILABLE → _propose_alternative_or_handoff`, `AVAILABLE → _handoff_after_scoping(free=True)`, and `None → _handoff_after_scoping(free=False)` = generic handoff. `_check_requested_slot` (`742-800`) returns `None` (→ plain handoff) for: calendar repo/`project_id` missing, calendar disabled, settings missing, **no parseable date+time in `intent.dates`**, or **not exactly one** active service rule.
  - **What this story changes.** Insert an "ask for time" branch *between* "scoping complete" and "generic handoff", gated to the calendar-actionable case and bounded to one ask via the new `awaiting_time` stage. The `None`-because-no-time sub-case of `_check_requested_slot` is the trigger; the `None`-because-disabled/no-service sub-cases must keep handing off (AC-4) — so the gate is computed separately, do not collapse them.
  - **Must preserve:** the busy→alternative path (`_propose_alternative_or_handoff`), free→confirm (`_handoff_after_scoping(free=True)`), the media-moment dispatch + `dispatch_fallback` plumbing, closure detection in pitching, `escalation_context`/`hitl_reason` on real handoffs, and the disabled/no-service generic handoff. A booking that already carries a concrete time must behave exactly as today (no extra turn).

- **`services/api/app/sales/intent.py`** *(read-only reference)* — `Intent` fields: `dates, headcount, vehicle_count, difficulty, drivers`; `is_complete()` requires all five non-None (so "no date at all" is already handled by scoping asking for `dates`; this story is specifically "`dates` present, but no concrete *time*"). `intent_merge` **replaces** non-None fields — the reason AC-5 asks for date+time together.

- **`services/api/app/calendar/service_resolver.py`** *(read-only reference)* — `extract_requested_start(text, *, now, project_tz)` returns a tz-aware `datetime` only when a concrete date **and** time parse out; date-only / vague text → `None`. This is the precise "no time" signal.

- **`services/api/app/calendar/requested_time_check.py`** *(read-only reference)* — the existing slot check the follow-up turn flows into unchanged.

### Conventions (project-context.md)

- Answerers **dispatch, never raise** into the pipeline; degraded-but-mine → escalate/handled, never silent fall-through. The ask-for-time result is a normal `handled=True` `AnswerResult` (not a skip).
- **Time is injected** — use `ctx.now`, never `datetime.now()` in branch logic (load-bearing for the 100% gate on time-edge branches).
- SQLite is sync, called via `await asyncio.to_thread(...)`; state persists through `_persist(ctx, current_stage, intent)` (columns: `current_stage`, `collected_intent_json`, …). **No new DB column needed** — the `awaiting_time` stage value itself is the "already asked" marker (keeps the bound stateless beyond the existing stage field).
- Public methods keyword-only; value objects `@dataclass(frozen=True)`; `from __future__ import annotations`; ruff `select = ["E","F","I"]`, line-length **100**; `fail_under = 100`.

### Testing standards

- `pytest` + `pytest-asyncio` (function-scoped loop), fakes via `Protocol` injection. Reuse the existing `_FakeCalSettings` / fake OpenRouter / fake state-repo harnesses already in `tests/test_sales_persona_answerer_scoping.py` and `..._pitching.py`. The follow-up-turn tests need a fake whose `complete_json` returns a `dates` with a concrete time on the second call.
- Cover every new branch: ask-fires, gated-off (disabled / not-exactly-one-service), awaiting_time→complete-on-time, awaiting_time→handoff-still-no-time, dispatch routing, and the `stage_before == AWAITING_TIME` skip.

### Project Structure Notes

- Pure api-side change in the sales persona (Epic 12) reusing the Epic 11/13 calendar surface. **No bot_gateway change, no api contract change, no new DB file/column.** Consistent with how the calendar-aware completion (`0373b5a`) was scoped.

### References

- [Calendar-aware booking completion — commit `0373b5a`] (the free/busy/alternative completion this story extends)
- [Source: services/api/app/sales/sales_persona_answerer.py#_complete_booking / _check_requested_slot / dispatch]
- [Source: services/api/app/sales/intent.py#intent_merge (replace semantics) / Intent.is_complete]
- [Source: services/api/app/calendar/service_resolver.py#extract_requested_start]
- [Source: _bmad-output/project-context.md#Critical Implementation Rules (answerers dispatch; time injected; 100% gate)]
- Product decision (this session): ask **only** when calendar enabled + exactly one active service; otherwise unchanged handoff.

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (Claude Code)

### Debug Log References

- Full suite + coverage (CI parity): **2931 passed, 100% coverage**; `ruff check .` clean.
- Surgical-change verification: before adding new tests, only the two calendar-active no-time pitching tests failed (145 other sales tests green), confirming the behaviour change is confined to the calendar-actionable + no-time path.
- Coverage edge: `_check_requested_slot`'s `requested_start is None → return None` (date present, time-less) is reachable **only** via the `awaiting_time` path (elsewhere `_should_ask_for_time` intercepts first) — covered by `test_awaiting_time_vague_date_still_no_time_hands_off`.

### Completion Notes List

- Added `STAGE_AWAITING_TIME` + `ASK_FOR_TIME_LINE`; dispatch routes it to the new `_handle_awaiting_time`.
- Factored the scoping LLM extract+merge into `_extract_and_merge` (reused by scoping + awaiting-time; preserves the scoping `fields_extracted` log).
- Factored the calendar gate into `_calendar_booking_context` (shared by `_check_requested_slot` + `_should_ask_for_time` — no drift).
- `_complete_booking` asks via `_ask_for_time` when `stage_before != awaiting_time` and `_should_ask_for_time` is true; bounded to one ask by the stage marker.
- Ask reply preserves the "never silent about failed media" invariant (appends `MATERIAL_DISPATCH_FALLBACK_LINE` when a dispatch failed).
- Scope choices: ask for date+time **together** (merge-replace safe); `awaiting_time` deliberately **not** in `_ASIDE_INTERCEPT_STAGES` (an aside mid-clarification re-extracts as no-time → hand off; acceptable for v1).

### Amendments

- **Copy reworded (2026-05-31, operator feedback).** `SCOPING_COMPLETE_HANDOFF_LINE` changed from `"Спасибо! Передам детали коллегам — подтвердят и вернутся с предложением."` to `"Спасибо! Передам детали коллегам на подтверждение — вернутся с ответом."`. Rationale: the bot must not promise colleagues *will* confirm (`подтвердят` presumes the outcome) — it only states the booking is passed *for* confirmation. Mirrors the existing `SLOT_FREE_HANDOFF_LINE` ("…передам коллегам для подтверждения.") and the sibling `PITCHING_ACCEPT_CONFIRM_LINE` reword (Story 12.19). Constant-only change; the 11 test files referencing `SCOPING_COMPLETE_HANDOFF_LINE` assert against the constant, so they tracked the new copy (`ruff` + full suite re-run green).

### File List

- `services/api/app/sales/sales_persona_answerer.py` (modified)
- `tests/test_sales_persona_answerer_awaiting_time.py` (new)
- `tests/test_sales_persona_answerer_pitching.py` (modified — 2 no-time tests now assert the ask)
- `_bmad-output/implementation-artifacts/12-10-ask-for-time-when-booking-has-no-time.md` (this story)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (added `12-10 … ready-for-dev`)
