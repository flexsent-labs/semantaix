# Story 12.96: The missing-date ask is deterministic, not LLM-dependent (round-27 R26-2)

Status: review

## Story
As a customer who gives a time but no date («можно в 16:00 на багги»), I want to be asked «на какую дату записать?» (keeping my 16:00), not have the time re-asked/dropped and end in a handoff — even when the LLM fails to capture the time.

## Root cause (CONFIRMED)
The round-26 `_has_time_without_date` is correct **only when the LLM folds the bare time into `intent.dates`**. Live, the LLM dropped «в 16:00» from `intent.dates`, so at completion `_has_time_without_date` saw no clock → False, and `_should_ask_for_time` re-asked for the date+time (dropping the given 16:00) → on re-supply, a handoff. The defect was the dependence on LLM extraction. Reproduced: with the LLM extracting `{}`, the booking never asked for the date.

## Fix (deterministic, story-12.43 raw-text-fallback pattern)
- `_has_time_without_date(ctx, intent, question)` now scans BOTH `intent.dates` AND the raw `question`: a clock present anywhere + no resolvable date anywhere → True. So the missing-date ask fires regardless of what the LLM extracted.
- `_fold_raw_time_into_intent`: when `intent.dates` lacks a clock but the raw question has one, store a normalized «в HH:MM» before asking — so `_preserve_prior_date` re-attaches the time on the follow-up date and the full slot is checked.
- Wired at the top of `_complete_booking` (passes `question`), before the bounded date+time ask.

## Acceptance Criteria
1. Time-with-no-date where the LLM extracted nothing → «на какую дату записать?», never a time re-ask/defer/handoff; the time is folded into the persisted intent. ✅
2. The follow-up bare date re-attaches the time → full slot checked (round-26 path still holds). ✅
3. No clock anywhere → unchanged (normal ask-for-time/handoff); a complete date+time booking is unaffected. ✅
4. Gates green; 100% coverage. ✅

## Files
- `services/api/app/sales/sales_persona_answerer.py` (`_has_time_without_date` raw-text scan, `_fold_raw_time_into_intent`, `_complete_booking` hook)
- tests: `test_sales_persona_answerer_early_busy_check.py`
