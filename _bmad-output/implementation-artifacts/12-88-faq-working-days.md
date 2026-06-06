# Story 12.88: Working-DAYS FAQ → answer, not booking (round-25 R25-2)

Status: review

## Story
As a customer who asks «А вы по воскресеньям работаете?», I want the schedule answered, not a booking handoff.

## Root cause (CONFIRMED)
The round-21 FAQ detectors covered working-HOURS («до скольки работаете?») but not working-DAYS — `is_working_hours_question` needs a «сколько/часы/когда» marker or a schedule noun, which a days question lacks. So «по воскресеньям работаете?» was `is_working_hours=False`/`is_info_faq=False` → it fell to the funnel → booking handoff. Same FAQ-misroute cluster as R20-1/2, R21-2.

## Fix
`is_working_days_question` (a work keyword + a day-scope marker: «по воскресеньям/выходным/будням/дням», «в выходные/будни», «какие дни», «по каким дням», «каждый день», «ежедневно») dispatches before the funnel (alongside working-hours) to `_handle_working_days_question`, which answers from `service_days` + `working_hours`: «Работаем ежедневно с 08:00 до 21:00.» (all 7 days) or lists the day abbreviations (пн, ср, пт…); defers as a question when the calendar is off.

## Acceptance Criteria
1. «по воскресеньям работаете?» / «в выходные работаете?» / «какие дни работаете?» → the schedule, never a booking handoff. ✅
2. Derived from config (deterministic); all-7 → «ежедневно», partial → listed days. ✅
3. «до скольки работаете?» (hours) and «сколько стоит?» are NOT mistaken for a days FAQ. ✅
4. Calendar off → defers as a question. ✅
5. Gates green; 100% coverage. ✅

## Files
- `services/api/app/sales/sales_persona_answerer.py` (`_DAYS_QUESTION_RE`, `is_working_days_question`, `_format_working_days`, `WORKING_DAYS_LINE`, `_handle_working_days_question`, dispatch branch)
- tests: `test_sales_persona_answerer_early_busy_check.py`
