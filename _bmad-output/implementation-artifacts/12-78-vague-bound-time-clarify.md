# Story 12.78: A vague lower/upper-bound time clarifies, never books a bare clock (round-18 R19-3)

Status: review

## Story
As a customer who gives a time range («завтра где-то после 15:00», «до 14:00», «не раньше 16:00»), I want the bot to propose a concrete slot inside that range and confirm it — never silently book the bare bound hour as if I'd committed to it.

## Root cause (CONFIRMED)
A bound phrase contains a parseable clock, so `extract_requested_start("завтра после 15:00")` → `2026-06-06 15:00` (the «после» was ignored), and `detect_vague_window` returned `None` for it. The slot check then ran on a concrete 15:00 and the booking was confirmed «свободно» at a time the customer never picked. This was inconsistent with the R14-1 vague-window fix («во второй половине дня»), which correctly clarifies.

## Fix
- `extract_time_bound(text)` returns `("after"|"before", hour)` for an open-ended bound; the number must look like a clock (`:MM` or «час…») so a date («до 6 июня») isn't mistaken for a bound.
- `extract_requested_start` returns `None` when a bound is present (the time is underspecified) — only the TIME; `extract_requested_date` still resolves the day.
- `detect_vague_window` maps a bound to a window («после 15:00» → (15, 22); «до 14:00» → (8, 14)), so the existing R14-1 `_maybe_answer_vague_window` path proposes the first free slot inside it and asks «Подойдёт или назовите удобное время?», parking in pitching. A booking is therefore never completed without a concrete `HH:MM`.

## Acceptance Criteria
1. «после 15:00» / «до 14:00» / «не раньше 16:00» → a proposed concrete slot + clarify, never a bare-bound completion. ✅
2. A plain «в 15:00» (no bound) stays a concrete commitment. ✅
3. «до 6 июня» (a date) is not mistaken for a time bound. ✅
4. The date is still parsed (only the time is underspecified). ✅
5. Gates green; 100% coverage. ✅

## Files
- `services/api/app/calendar/service_resolver.py` (`_LOWER_BOUND_RE`, `_UPPER_BOUND_RE`, `extract_time_bound`, open-bound short-circuit in `extract_requested_start`)
- `services/api/app/sales/sales_persona_answerer.py` (`detect_vague_window` bound handling)
- tests: `test_calendar_service_resolver.py`, `test_sales_persona_answerer_early_busy_check.py`
