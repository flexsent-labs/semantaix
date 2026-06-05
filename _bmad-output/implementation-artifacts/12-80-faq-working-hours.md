# Story 12.80: Answer a working-hours FAQ, not a booking handoff (round-20 R20-1)

Status: review

## Story
As a customer who asks «до скольки вы работаете?», I want a direct answer («Работаем с 08:00 до 18:00»), not a booking handoff or a date/time ask.

## Root cause (CONFIRMED)
«до скольки работаете?» is `is_sales_intent=True` and `classify=other`, so it fell through to the booking funnel → handoff/date-ask. No FAQ branch existed. Same over-eager-handoff family as R16/R18-6. The working hours are available on the calendar service rule (`working_hours`), which the bot already uses for off-hours verdicts.

## Fix
`is_working_hours_question` (work keyword + a «сколько/часы/когда» marker, or a schedule noun «график/режим/часы работы»/«расписание») dispatches before the funnel to `_handle_working_hours_question`, which formats the overall open/close span from `cal.service_rule.working_hours` («Работаем с 08:00 до 18:00.»). When the calendar is off / has no hours, it defers as a question (Story 12.81), never a booking.

## Acceptance Criteria
1. «до скольки работаете?» / «во сколько открываетесь?» / «график работы?» → the hours, never a handoff or date ask. ✅
2. Hours derived from config (deterministic, no LLM). ✅
3. «сколько стоит?» / «сколько длится?» are NOT mistaken for a hours FAQ. ✅
4. Calendar off → defers as a question. ✅
5. Gates green; 100% coverage. ✅

## Files
- `services/api/app/sales/sales_persona_answerer.py` (`is_working_hours_question`, `_format_working_hours`, `WORKING_HOURS_LINE`, `_handle_working_hours_question`, dispatch branch)
- tests: `test_sales_persona_answerer_early_busy_check.py`
