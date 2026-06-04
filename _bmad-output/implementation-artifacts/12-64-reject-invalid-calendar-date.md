# Story 12.64: Reject an impossible calendar date (round-16 R16-1)

Status: review

## Story
As a customer who mistypes a date («31 июня», «31.06», «30 февраля»), I want the bot to tell me it doesn't exist and ask again, so I don't get a silent booking handoff on a date that can't happen.

## Root cause (CONFIRMED)
`extract_requested_start` runs an impossible day+month through `_safe_date` → `ValueError` → `None`. So an invalid date is **indistinguishable from "no date given"**, and the booking flow treats it as "time unverifiable" → a handoff. There was no signal for "you named a day that doesn't exist."

## Fix
`names_invalid_date(text, now, project_tz)` in `service_resolver.py` — True when a Russian "<day> <month>" or a "DD.MM" (month 1-12) names a day beyond that month's length (`calendar.monthrange`; leap-Feb uses `now`'s year). A "HH.MM" clock is excluded (its second number isn't a valid month). Dispatched **before** the funnel: `_handle_invalid_date` → «Такой даты не существует. Уточните, пожалуйста, желаемую дату.» (localized), no escalation, funnel state intact.

## Acceptance Criteria
1. «31 июня» / «31.06» / «30 февраля» → a clarify, never a booking handoff. ✅
2. A valid date («5 июня», «31 мая») is unaffected. ✅
3. A clock «в 14.30» is not mistaken for a date. ✅
4. Gates green; 100% coverage. ✅

## Files
- `services/api/app/calendar/service_resolver.py` (`names_invalid_date`)
- `services/api/app/sales/sales_persona_answerer.py` (`INVALID_DATE_CLARIFY_LINE` + dispatch branch + `_handle_invalid_date`)
- tests: `test_calendar_service_resolver.py`, `test_sales_persona_answerer_early_busy_check.py`
