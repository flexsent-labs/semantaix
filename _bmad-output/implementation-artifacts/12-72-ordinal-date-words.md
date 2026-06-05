# Story 12.72: Parse ordinal date words «девятого» (round-18 R18-4)

Status: review

## Story
As a customer who names a day by its ordinal word («девятого в 12:00», «двадцать третьего»), I want the bot to resolve it to the right calendar date so the availability verdict is correct — not silently dropped, yielding a wrong "свободно".

## Root cause (CONFIRMED)
`extract_requested_start` had no ordinal-day grammar. Reproduced: `extract_requested_start("девятого в 12:00")` → `None`, while the numeric `"9 июня в 12:00"` → `2026-06-09 12:00`. So the ordinal date was dropped, the requested date defaulted/went stale, and the slot check ran on the wrong date — reporting a busy slot (9 June 12:00 ∈ the 11–13 block) as free.

## Fix
`_extract_ordinal_date` resolves Russian genitive ordinals 1-31, including the compound «двадцать/тридцать <unit>» («двадцать третьего» → 23), to the **next occurrence** of that day-of-month (this month if it hasn't passed, else a following month; scans ~13 months so day numbers absent from short months still resolve, and rolls the year at December). Wired as a fallback in both `extract_requested_start` and `extract_requested_date`, after the «<day> <month>» (RU/EN) forms.

## Acceptance Criteria
1. «девятого в 12:00» resolves identically to «9 июня в 12:00» → same (занято) verdict. ✅
2. «пятого» (today) → today; «первого» (passed) → next month; «двадцать третьего» → 23rd. ✅
3. An impossible ordinal («тридцать девятого» → 39) declines rather than guessing. ✅
4. Year rolls correctly (December «первого» → 1 Jan next year). ✅
5. Gates green; 100% coverage. ✅

## Files
- `services/api/app/calendar/service_resolver.py` (`_ORDINAL_*` maps/regexes, `_ordinal_day`, `_extract_ordinal_date`, wired into both extractors)
- tests: `test_calendar_service_resolver.py`
