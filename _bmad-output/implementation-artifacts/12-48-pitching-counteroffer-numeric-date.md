# Story 12.48: A numeric counter-offer in pitching must be busy-checked (round-10 N1, P2)

Status: review

## Story

As a **customer who, after a busy slot, counter-offers a new dotted date («Можно 03.06 в 16:00 на багги, нас двое?»)**, I want **that new time checked against the calendar**, so that **I'm told «занято» when it's taken — not handed off as if the booking were fine.**

**Problem (live, round 10, 2 Jun 2026, 20:48):** after the bot offered an alternative and parked in `pitching`, the customer countered with «Можно 03.06 в 16:00 …» (16:00 on 3 June is busy). The bot replied with the generic handoff — the new time was accepted with **no calendar check**.

## Root cause (CONFIRMED)

The pitching counter-offer detector used the wrong parser. In `_handle_pitching`, a new date is adopted via `_merge_dates_from_customer_message`, which gated on `parse_russian_date_span` (the `date_parser` helper) — and that helper has **no numeric-date support**. A dotted `03.06` therefore parsed to `None`, so the counter-offer was treated as "no new date", the existing (already-offered) intent rode through, and `_complete_booking` short-circuited to a handoff without re-checking the slot. The early-busy-check (12.25) and the numeric raw-text fallback (12.43) both run on the *scoping* path; the *pitching* counter-offer path never gained numeric parsing.

## Fix

Teach `_merge_dates_from_customer_message` the same deterministic parser the rest of the funnel uses. A counter-offer is adopted when **either** `parse_russian_date_span` **or** the deterministic `extract_requested_start` (the numeric/relative/weekday parser behind the busy check) finds a concrete start in the message. The new `_question_carries_concrete_start(question, now)` helper calls `extract_requested_start` with the project tz from `now.tzinfo`; when it (or the legacy span parser) sees a date, the raw question is adopted as `intent.dates`, so `_complete_booking` → `_check_requested_slot` re-parses it and the busy verdict fires. Date phrasing/optional fields don't matter — the same parser that powers «3/6 в 16:00» now powers «03.06 в 16:00».

## Acceptance Criteria

1. A pitching counter-offer with a dotted date + separate clock («Можно 03.06 в 16:00 …», 16:00 busy) → «занято» + nearest-free, identical to «3/6 в 16:00» / «завтра в 16:00». ✅
2. The calendar IS queried on such a counter-offer (no silent accept). ✅ (`freebusy.calls == 1`)
3. A pitching turn with no concrete start still leaves the intent unchanged (no false counter-offer). ✅ (default path preserved)
4. Russian conversations unchanged; gates green; 100% coverage. ✅

## Tasks / Subtasks

- [x] `_question_carries_concrete_start` helper (tz-aware `extract_requested_start`) + extend `_merge_dates_from_customer_message` to adopt on EITHER parser.
- [x] Test (TDD): pitching state + «Можно 03.06 в 16:00 …» (3 June afternoon busy) → `SLOT_BUSY_LINE`, `freebusy.calls == 1`.

## Dev Notes

- The legacy `parse_russian_date_span` is kept as the first check (cheap, covers written-month spans); `extract_requested_start` is the additive numeric/relative path. Both feed the SAME `replace(existing_intent, dates=question.strip())` adoption.
- `now.tzinfo` is always set on the sales path (the clock returns an aware datetime), so the tz guard inside the helper is satisfied; a naive `now` would simply skip the numeric branch.
- **Files:** `services/api/app/sales/sales_persona_answerer.py`.

## References

- Round-10 live QA Defect N1. Builds on Story 12.38 (numeric parsing) and 12.43 (numeric raw-text fallback on the scoping path).
- [Source: sales_persona_answerer.py#_merge_dates_from_customer_message], [#_question_carries_concrete_start], [calendar/service_resolver.py#extract_requested_start].

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–4).** The pitching counter-offer path now parses numeric/dotted dates via `extract_requested_start`, so a busy counter-offer is told «занято» instead of being silently accepted. TDD; full suite green at 100% coverage.

### File List

- `services/api/app/sales/sales_persona_answerer.py` (modified — `_question_carries_concrete_start` + `_merge_dates_from_customer_message`)
- `tests/test_sales_persona_answerer_early_busy_check.py` (modified — pitching numeric counter-offer busy-check test)
- `_bmad-output/implementation-artifacts/12-48-pitching-counteroffer-numeric-date.md` (new)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified)
