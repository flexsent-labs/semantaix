# Story 12.67: Per-day verdict for multi-option dates (round-16 R16-2)

Status: review

## Story
As a customer asking «в субботу или в воскресенье в 12:00…?», I want to know which day is free, not a single unstated «свободно».

## Root cause (CONFIRMED)
`extract_requested_start` picks the FIRST date (суббота) and ignores «или воскресенье» → one verdict, the day unstated. (Also surfaced a latent bug: «воскресенье» lemmatizes to «воскресение», which wasn't in the weekday map, so Sunday never parsed at all.)

## Fix
- `_maybe_answer_multi_date` (greeting + scoping intercept, gated on «или»): split on «или», parse a date per side + a shared clock, check each via `check_requested_availability`, and reply an explicit per-day verdict — «6 июня в 12:00 - свободно; 7 июня в 12:00 - свободно. Какой день вам удобнее?». Russian-only by design (the «или» gate). `None` (falls through) unless two distinct dates + a clock + both verifiable.
- Added «воскресение» to `_WEEKDAYS` so Sunday resolves (regression-guarded).

## Acceptance Criteria
1. Two date options + a time → an explicit per-day verdict naming both days. ✅
2. One busy / one free → distinct per-day verdicts. ✅
3. No time, or a single date → falls through to the normal flow. ✅
4. «воскресенье» now resolves to Sunday. ✅
5. Gates green; 100% coverage. ✅

## Files
- `services/api/app/calendar/service_resolver.py` (`_WEEKDAYS` Sunday lemma)
- `services/api/app/sales/sales_persona_answerer.py` (`_maybe_answer_multi_date` + greeting/scoping hooks)
- tests: `test_calendar_service_resolver.py`, `test_sales_persona_answerer_early_busy_check.py`
