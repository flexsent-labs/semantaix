# Story 12.73: Reject an explicit past date (round-18 R18-1)

Status: review

## Story
As a customer who names a past day («запишите на вчера в 14:00»), I want to be told the date is in the past and asked again — never handed off as a valid booking into the past.

## Root cause (CONFIRMED)
«вчера»/«позавчера» weren't in `_RELATIVE_DAYS` (only сегодня/завтра/послезавтра), so `extract_requested_start("вчера в 14:00")` → `None` → unverifiable → generic handoff. Past-*time-today* is caught by the calendar's `REASON_IN_PAST`, but a whole past *day* never got parsed to reach that check.

## Fix
- «вчера» → -1 / «позавчера» → -2 added to the relative-day offsets (so a past day is recognised).
- `names_past_date(text, now, project_tz)` returns True when the text resolves (via `extract_requested_date`) to a day strictly before today. Numeric/absolute dates roll forward, so in practice only the explicit past-day words trigger it.
- Dispatched **before** the funnel (right after the invalid-date guard): `_handle_past_date` → «Эта дата уже прошла. Уточните, пожалуйста, желаемую дату.» (localized), no escalation, funnel intact. Calendar-independent — rejects even when the calendar is disconnected.

## Acceptance Criteria
1. «вчера в 14:00» / «позавчера» → past-date clarify, never a booking handoff. ✅
2. «завтра» / «сегодня» / «9 июня» (future or today) are unaffected. ✅
3. Deterministic (no LLM), `suppress_followup`, no escalation. ✅
4. Gates green; 100% coverage. ✅

## Files
- `services/api/app/calendar/service_resolver.py` (`_RELATIVE_DAYS` past words, `names_past_date`)
- `services/api/app/sales/sales_persona_answerer.py` (`PAST_DATE_CLARIFY_LINE`, dispatch branch, `_handle_past_date`)
- tests: `test_calendar_service_resolver.py`, `test_sales_persona_answerer_early_busy_check.py`
