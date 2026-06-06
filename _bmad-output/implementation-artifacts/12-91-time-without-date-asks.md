# Story 12.91: A time with no date asks for the date (round-26 R26-2)

Status: review

## Story
As a customer who gives a time but no date («можно в 16:00 на багги?»), I want to be asked «на какую дату?», not get a defer as if a slot were being checked.

## Root cause (CONFIRMED)
Reproduced: a first-turn «в 16:00» correctly asks via the LLM next-question. But once the booking reaches `_complete_booking` in `STAGE_AWAITING_TIME`, the date+time ask is bounded to one round (`stage_before != AWAITING_TIME`), so a follow-up time-with-no-date skips the ask and falls through to a defer/handoff — never re-asking for the date.

## Fix
- `_has_time_without_date` (calendar-actionable + a clock present in `intent.dates` + no resolvable date) is checked at the top of `_complete_booking`, BEFORE the bounded ask, so it fires in ANY stage → `_ask_for_date` («Уточните, пожалуйста, на какую дату записать?»), parking in awaiting_time. Never a dateless defer.
- `_preserve_prior_date` generalised: it already re-attached a prior DATE to a bare-time reply; now it also re-attaches a prior TIME to a bare-DATE reply — so after «в 16:00» → «на какую дату?» → «7 июня», the kept time 16:00 combines into «7 июня 16:00» and the slot is checked.

## Acceptance Criteria
1. A turn with a time but no resolvable date → «на какую дату записать?», never a defer/handoff. ✅
2. The follow-up bare date re-attaches the time and the full slot is checked. ✅
3. A complete date+time booking is unaffected. ✅
4. Gates green; 100% coverage. ✅

## Files
- `services/api/app/sales/sales_persona_answerer.py` (`ASK_FOR_DATE_LINE`, `_has_time_without_date`, `_ask_for_date`, `_preserve_prior_date` bidirectional, `_complete_booking` hook)
- tests: `test_sales_persona_answerer_early_busy_check.py`
