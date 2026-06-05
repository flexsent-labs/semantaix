# Story 12.79: Parse half-form times «полвторого» → 13:30 (round-19 R19-2)

Status: review

## Story
As a customer who says a colloquial half-hour («завтра в полвторого»), I want it understood as 13:30 instead of being asked to specify the time.

## Root cause (CONFIRMED)
The clock scanner had no «пол<ordinal>» grammar, so `extract_requested_start("завтра в полвторого")` → `None` → time re-ask. Word-form family (R17-1 / R18-4).

## Fix
`_HALF_FORM_RE` (reusing the round-18 ordinal vocabulary) parses «пол<ordinal>» = 30 min before that hour: «полвторого» → 1:30, «полпервого» → 12:30, «полдвенадцатого» → 11:30. AM/PM is resolved per the chosen **daytime-default** rule:
- a day-part qualifier pins it: «полвторого дня» → 13:30, «полвторого ночи» → 01:30;
- a bare early hour (1-7) defaults to the daytime (PM) reading: «полвторого» → 13:30, while «полдевятого» (8) stays 08:30.

An ordinal above 12 («полдвадцатого») isn't an hour and is ignored.

## Acceptance Criteria
1. «полвторого» → 13:30; «полвторого дня» → 13:30; «полвторого ночи» → 01:30. ✅
2. «полпервого» → 12:30; «полдевятого» → 08:30 (morning, not promoted). ✅
3. «полдвадцатого» (invalid hour) → no clock. ✅
4. Gates green; 100% coverage. ✅

## Files
- `services/api/app/calendar/service_resolver.py` (`_HALF_FORM_RE`, half-form group in `_scan_clocks`)
- tests: `test_calendar_service_resolver.py`

## Note (R19-1 — global calendar blocking)
Confirmed business rule: calendar blocking is **global per day/time** (one operator, one activity at a time), not per-service — a quadbike event correctly blocks buggy. No code change; a guard test (`test_global_calendar_block_is_shared_across_services`) records the decision. (freeBusy returns time-only intervals with no event titles, so per-service blocking would need per-service calendars or the Events API — deferred unless the fleets become independent resources.)
