# Story 12.76: No-colon compact time «в 1400» (round-18 R18-3)

Status: review

## Story
As a customer who types a time without a colon («завтра в 1400», «в 14 00»), I want it understood as 14:00 instead of being asked to clarify.

## Root cause (CONFIRMED)
The clock scanner only matched «HH:MM»/«HH.MM», am/pm, word-form, and «N часов». A bare «1400» / «14 00» matched none → `extract_requested_start("завтра в 1400")` → `None` → the bot asked for the time.

## Fix
`_COMPACT_HHMM_RE` adds a no-colon compact form **after «в»**: «в 1400» → 14:00, «в 14 00» → 14:00, «в 900» → 09:00. The «в» prefix plus a no-more-digits guard keep it from eating counts/prices («в 14000», «5 человек»); out-of-range values («в 2500») are rejected. Added as the lowest-priority group in the shared `_scan_clocks`.

## Acceptance Criteria
1. «в 1400» / «в 14 00» → 14:00; «в 900» → 09:00. ✅
2. «в 2500» (25:00) is out of range → no clock. ✅
3. Doesn't shadow «в 14:00» (HH:MM still wins) or eat longer digit runs. ✅
4. Gates green; 100% coverage. ✅

## Files
- `services/api/app/calendar/service_resolver.py` (`_COMPACT_HHMM_RE`, in `_scan_clocks`)
- tests: `test_calendar_service_resolver.py`
