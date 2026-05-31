# Story 12.25: Early busy-check during scoping (intercept conflict before asking the next field)

Status: review

## Story

As a **customer whose opener carries a concrete date and time**,
I want the bot to **tell me right away that the time is busy and offer the nearest free alternative**,
so that **I am not made to answer three or four logistics questions about a booking that was never going to happen at that time**.

**Problem (observed live, багги, 31 May 2026 13:36, Artur Yaskevich):**

```
Artur:  хочу забронировать багги завтра в 14:00
Анна:   Сколько человек поедет?
Artur:  2
Анна:   Сколько багги вам потребуется?
```

"Анна Иванова" is the sales bot persona. The opener carries a concrete `requested_start` (`завтра в 14:00`), but the bot drops into the scoping funnel and asks the remaining `Intent` fields (headcount → vehicle_count → difficulty → drivers) without ever validating the slot. The customer has to finish the funnel before the busy line is finally emitted.

**Root cause** (`services/api/app/sales/sales_persona_answerer.py`): the busy-check landed in Story 12.22 lives inside `_complete_booking` (`:1069`) — invoked only at `_handle_scoping:889` *after* `merged.is_complete(required)`. So a one-field opener (just `dates`) skips the check entirely: `_handle_greeting:675` persists `STAGE_SCOPING` at `:712` and returns the LLM's next-field question, and the first scoping turn at `:853` does the same when scoping is still incomplete. The check has nowhere to fire until every field is collected.

## Acceptance Criteria

1. **Greeting opener: busy + alternative.** Opener `"хочу забронировать багги завтра в 14:00"` with 14:00 tomorrow busy and an earlier slot free → the bot's first reply contains `SLOT_BUSY_LINE` + `"Ближайшее свободное время — …"`, persists `current_stage=STAGE_PITCHING` with `last_proposal={"alternative_iso": …}`, **does not** escalate to HITL on this turn (Story 12.22 contract preserved), **does not** ask any scoping field.
2. **Greeting opener: busy + no alternative.** Same opener but the calendar finds no free slot in the lookahead window → immediate `_handoff_after_scoping`-shaped escalation: `response_mode=RESPONSE_MODE_SALES_ESCALATION`, `escalate=True`, `hitl_reason='sales_scoping_complete'`, sticky `STAGE_PITCHING`.
3. **Greeting opener: available time → silent fallthrough.** Opener carries a free time → original `_handle_greeting` flow runs end-to-end: `STAGE_SCOPING`, LLM next-field question delivered, no mention of availability in the bot's reply, no `last_proposal` persisted.
4. **Scoping turn: time first appears mid-funnel + busy.** Turn 1: customer says `"хочу багги для двоих"` (LLM extracts `headcount=2`, no dates) → bot asks the next field. Turn 2: customer says `"завтра в 14:00"` and that time is busy → bot intercepts: propose alternative, persist `STAGE_PITCHING`, partial intent (headcount=2, dates="завтра в 14:00") preserved.
5. **Scoping turn: time unchanged → no re-check.** Turn 1: opener already has `dates="завтра в 14:00"` AND `headcount=2`; 14:00 is available; bot asks next field. Turn 2: customer answers vehicle_count; the parsed `requested_start` is identical to the previous turn → `check_requested_availability` MUST NOT be called this turn (assert via a counter on the fake free-busy client).
6. **Calendar disabled fall-through.** Project calendar disabled → `_maybe_intercept_busy_slot` returns `None`. Original `_handle_greeting` / `_handle_scoping` flow unchanged. No availability call made.
7. **Multi-service project fall-through.** Project has two or more active calendar service rules → `_calendar_booking_context` returns `None` → helper returns `None`. Scoping continues as today (a future story may add per-service routing here).
8. **NOT_CONNECTED / ERROR fall-through.** Calendar reports `STATUS_NOT_CONNECTED` or `STATUS_ERROR` → helper returns `None`. Mirrors `_complete_booking:1112` precedent (those statuses fall to `_handoff_after_scoping(free=False)` at scoping completion, never to a HITL ticket from the early gate).
9. **Pitching follow-up regression-free.** After the early gate parks in pitching, the existing 12.19 / 12.22 flow handles the next turn unchanged: `"да"` confirms the offered slot → escalate once via `_confirm_slot`; `"давайте на 16:00"` is a counter-offer → re-runs `_complete_booking(stage_before=STAGE_PITCHING)`; `"всё, спасибо"` hands off via `_handoff_after_pitching_followup`. Existing tests under `test_sales_persona_answerer_pitching_reask_guard.py` and `test_sales_persona_answerer_propose_alternative_defers_hitl.py` must remain green.
10. **Gates green.** `ruff check .` clean; full suite at 100% coverage on `platform_common/` + `services/` (`pytest --cov --cov-config=.coveragerc`).

## Tasks / Subtasks

- [ ] **Helper `_maybe_intercept_busy_slot`** (AC 1,2,3,4,5,6,7,8) — new private async method on `SalesPersonaAnswerer` taking `(ctx, existing_intent, merged_intent, stage_before)`. Resolves the calendar booking context via the existing `_calendar_booking_context(ctx)`; bails to `None` if not actionable. Compares `extract_requested_start(existing_intent.dates, …)` to `extract_requested_start(merged_intent.dates, …)` — parsed `datetime`s, not strings — and returns `None` unless the new value is concrete AND different. Calls `check_requested_availability` mirroring `_check_requested_slot:1222`; only `STATUS_UNAVAILABLE` triggers `_propose_alternative_or_handoff(ctx, intent=merged_intent, stage_before, alternative, base_metadata={}, dispatch_fallback=False)`. Emits a `sales_early_busy_intercepted` log line with `trace_id`, `stage_before`, `requested_start_iso`, and `alternative_iso`.
- [ ] **Hook into `_handle_greeting`** (AC 1,2,3) — at `:712`, between `merged = intent_merge(Intent(), extracted)` and the `_persist(STAGE_SCOPING, …)` call, invoke the helper with `existing_intent=Intent()` (greeting has no prior state; Story 12.23's stale-closing reset is consistent — STAGE_DORMANT / STAGE_CLOSING that re-enter greeting are semantically a fresh funnel). Return the helper's `AnswerResult` if non-None; otherwise fall through to the existing persist + `next_question`.
- [ ] **Hook into `_handle_scoping`** (AC 4,5) — at the top of the `if not merged.is_complete(required):` branch at `:853`, before `_persist`. Uses the `existing` Intent already captured at `:806` so the idempotency guard sees the prior parse. Return the helper's `AnswerResult` if non-None; otherwise the existing persist + next-field-question continues unchanged.
- [ ] **TDD test file** (AC 1–9) — new `tests/test_sales_persona_answerer_early_busy_check.py`. Mirror the fixture style of `tests/test_sales_persona_answerer_pitching_reask_guard.py` (`_FakeStateRepo`, `_FakeCalSettings`, `_TokenProvider`, `_FreeBusy`, `_NOW`) and the LLM-queue style of `tests/test_sales_persona_answerer_greeting.py` (`_FakeOpenRouter.queue_response`). Required cases (one test per AC plus a counter-on-`_FreeBusy` instrumentation): greeting busy+alt; greeting busy+no-alt; greeting available; scoping turn-2 time-change to busy; scoping turn-2 time unchanged → no re-check (counter == 1); calendar disabled fall-through; multi-service fall-through; NOT_CONNECTED fall-through; ERROR fall-through.
- [ ] **Gates** (AC 10) — `ruff check .` clean; full suite at 100% coverage via the main-repo venv (`/Users/aj/workspace_ai/semaintix/.venv/bin/python -m pytest --cov --cov-config=.coveragerc --cov-report=term-missing`).

## Dev Notes

- **Files:** `services/api/app/sales/sales_persona_answerer.py` (`_handle_greeting:675`, `_handle_scoping:794`, new helper); `tests/test_sales_persona_answerer_early_busy_check.py` (new). No new constants exported, no data-file changes, no schema changes.
- **Reuse, don't reinvent:** `_calendar_booking_context` (`:1162`) — already gates calendar-enabled + single active service; reuse verbatim. `extract_requested_start` (`services/api/app/calendar/service_resolver.py:172`) returns a tz-aware `datetime` only when both a clock and a day anchor are parseable — exactly the "concrete time" signal we need. `check_requested_availability` (`services/api/app/calendar/requested_time_check.py:86`) is shared with `_check_requested_slot`. `_propose_alternative_or_handoff` (`:1264`) renders `SLOT_BUSY_LINE + " Ближайшее свободное время — D MMMM, HH:MM."`, persists `STAGE_PITCHING` + `last_proposal`, and branches between non-escalating-with-alternative and escalating-without (Story 12.22). `_format_intent_summary` (`escalation_context`) handles a partial `Intent` without choking — `None` fields are skipped.
- **Why compare parsed `datetime`s, not strings:** `Intent.dates` is a free-form Russian string. `"14:00 завтра"` and `"завтра в 14:00"` parse to the same `datetime` but differ as strings; comparing the parses prevents a spurious re-check.
- **Why `Intent()` for greeting:** `_handle_greeting` does not load state today (`:675`). Reading state just to confirm "empty for STAGE_NEW" costs a roundtrip for no signal. Story 12.23's stale-closing reset also funnels resumes through greeting with a semantically fresh funnel — passing `Intent()` is consistent.
- **Why AVAILABLE stays silent:** an explicit acknowledgement ("Хорошо, 14:00 свободно — …") duplicates the next-field question with a load-bearing claim that the calendar could invalidate before the customer finishes scoping. Keep the existing UX; the helper is a busy-only interceptor.
- **Why NOT_CONNECTED / ERROR fall through silently:** `_complete_booking:1102` already treats only `STATUS_UNAVAILABLE` as busy; other statuses route to `_handoff_after_scoping(free=False)` at scoping completion. Escalating from the *early* gate on infra hiccups would be more aggressive than the existing late-gate path. The late gate will re-run the same check after scoping completes anyway.
- **Known accepted limitation:** if the customer counter-offers a new time during pitching that turns out to be free, `_complete_booking` escalates with the *partial* `Intent` collected so far (only `dates`, no headcount/vehicle_count). This mirrors Story 12.22's spirit — the operator finishes the booking by phone — and is preferable to silently looping back into scoping. No regression: this is exactly what happens today when scoping completion triggers `_handoff_after_scoping(free=True)` for any intent shape.
- **Conventions:** answerer dispatch never raises; immutable `Intent` (`replace`); time injected; `from __future__ import annotations`; ruff E/F/I line-100; 100% coverage gate on `services/` + `platform_common/`.

### Project Structure Notes

- All changes are localized to the sales answerer + a new test file. No new data layer, no migrations, no settings changes. The existing `last_proposal` round-trip through `state_repository.upsert` / `get` is unchanged.

### References

- [Source: services/api/app/sales/sales_persona_answerer.py#_handle_greeting `:675`] · [Source: ...#_handle_scoping `:794`] · [Source: ...#_complete_booking `:1069`] · [Source: ...#_calendar_booking_context `:1162`] · [Source: ...#_check_requested_slot `:1222`] · [Source: ...#_propose_alternative_or_handoff `:1264`] · [Source: ...#_handle_pitching `:928`] · [Source: ...#_confirm_slot `:971`].
- [Source: services/api/app/calendar/requested_time_check.py#check_requested_availability `:86`] · [Source: services/api/app/calendar/service_resolver.py#extract_requested_start `:172`].
- Precedent — story doc format: [`12-19-recognize-slot-confirmation-and-stop-pitching-reask-loop.md`](12-19-recognize-slot-confirmation-and-stop-pitching-reask-loop.md).
- Precedent — the late-gate this story brings forward: [`12-22-defer-hitl-until-alternative-accepted.md`](12-22-defer-hitl-until-alternative-accepted.md).
- Precedent — fixture style to mirror: [`tests/test_sales_persona_answerer_pitching_reask_guard.py`](../../tests/test_sales_persona_answerer_pitching_reask_guard.py) (`_FakeCalSettings`, `_TokenProvider`, `_FreeBusy`) and [`tests/test_sales_persona_answerer_greeting.py`](../../tests/test_sales_persona_answerer_greeting.py) (`_FakeOpenRouter.queue_response`).

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–10).** New `_maybe_intercept_busy_slot` helper on `SalesPersonaAnswerer` resolves the calendar booking context via the existing `_calendar_booking_context`, parses both `existing_intent.dates` and `merged_intent.dates` through `extract_requested_start`, gates the availability call on "new concrete value different from the prior parse" (comparing parsed `datetime`s, not strings), then routes `STATUS_UNAVAILABLE` to the existing `_propose_alternative_or_handoff`. `STATUS_AVAILABLE` / `STATUS_NOT_CONNECTED` / `STATUS_ERROR` all return `None` so the original `_handle_greeting` / `_handle_scoping` flow continues unchanged (mirrors `_complete_booking:1112`).
- **Hooks installed at two sites:** `_handle_greeting` (before the persist to `STAGE_SCOPING`, passing `Intent()` as `existing_intent`) and `_handle_scoping` (at the top of the `not merged.is_complete(required)` branch, reusing the already-captured `existing` Intent). `_handle_awaiting_time` and `_complete_booking` are untouched — the late gate still runs unchanged at scoping completion.
- **TDD red→green:** the nine tests in `test_sales_persona_answerer_early_busy_check.py` were written and run first; four failed for the expected reason ("the bot still asks the next field / the early gate does not fire") and five (fall-through cases) passed against the unchanged code. After the helper + hooks shipped, 9/9 are green. No existing test required adjustment — the helper is purely additive and the `_propose_alternative_or_handoff` contract from Stories 12.19 and 12.22 is reused verbatim.
- **Structured log:** new `sales_early_busy_intercepted` line carries `trace_id`, `stage_before`, `requested_start_iso`, and `alternative_iso` so operators can trace early-gate interceptions distinctly from the late `_complete_booking` path.
- `ruff check .` clean; full suite **3054 passed at 100% coverage** (`pytest --cov --cov-config=.coveragerc`).

### File List

- `services/api/app/sales/sales_persona_answerer.py` (modified — new `_maybe_intercept_busy_slot` helper; call sites in `_handle_greeting` + `_handle_scoping`)
- `tests/test_sales_persona_answerer_early_busy_check.py` (new — 9 tests covering AC 1–9)
- `_bmad-output/implementation-artifacts/12-25-early-busy-check-during-scoping.md` (new — this story doc)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified — added `12-25-early-busy-check-during-scoping: ready-for-dev`)
