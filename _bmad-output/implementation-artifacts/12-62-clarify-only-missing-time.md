# Story 12.62: Ask only for the missing time when the date is known (round-15)

Status: review

## Story
As a customer who already named the date, I want to be asked only for the time, not «дату и время» again.

## Root cause / why deferred until now
`ASK_FOR_TIME_LINE` deliberately asked for date+time because `intent_merge` **replaces** the `dates` field — a bare follow-up «в 15:00» would overwrite and drop the previously-collected date (Story 12.10's note). So the safe fix needs date preservation, not just a shorter prompt.

## Fix
1. `_ask_for_time` picks `ASK_FOR_TIME_ONLY_LINE` («Уточните, пожалуйста, желаемое время…») when `extract_requested_date(intent.dates)` is non-None; otherwise the date+time line.
2. `_preserve_prior_date` (called in `_handle_awaiting_time`): when the reply parsed to a time-only `dates` (no full date) and the prior stored `dates` had a parseable date, rebuild `"<YYYY-MM-DD> HH:MM"` so the slot check gets a full date+time instead of dropping the date.

## Acceptance Criteria
1. Date known + no time → ask only «время». ✅ (also covers the pitching counter-offer «1 июня» case)
2. The bare-time reply re-attaches the prior date → a real busy/free verdict (not a blind handoff). ✅
3. No date known → still ask «дату и время». ✅
4. Gates green; 100% coverage. ✅

## Files
- `services/api/app/sales/sales_persona_answerer.py` (`ASK_FOR_TIME_ONLY_LINE` + `_ask_for_time` branch + `_preserve_prior_date`)
- tests: early-busy-check (preserve-date), pitching + reask-guard (only-time assertions updated)
