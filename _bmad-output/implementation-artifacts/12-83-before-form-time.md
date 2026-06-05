# Story 12.83: Parse «без пятнадцати три» → 14:45 (round-20 R20-3)

Status: review

## Story
As a customer who says «завтра без пятнадцати три» (a quarter to three), I want it understood as 14:45 instead of being asked to specify the time.

## Root cause (CONFIRMED)
The clock scanner had no «без <minutes> <hour>» grammar, so `extract_requested_start("завтра без пятнадцати три")` → `None` → time re-ask. Word-form family (R17-1 / R19-2).

## Fix
`_BEFORE_RE` parses «без <minutes> <hour>» = N min before the named (upcoming) hour: «без пятнадцати три» → 2:45 → daytime 14:45; «без четверти восемь» → 19:45; «без десяти час» → 12:50. Minutes are the common genitive forms incl. «четверти» (quarter = 15); the hour is a cardinal word or «час» (= 1). AM/PM follows the round-19 daytime-default rule (bare early hours promote to PM; a day-part pins it: «без пятнадцати три ночи» → 02:45). The `before` group outranks the day-part group so «без … три ночи» isn't mis-read as «три ночи».

## Acceptance Criteria
1. «без пятнадцати три» → 14:45; «без четверти восемь» → 19:45; «без десяти час» → 12:50. ✅
2. A day-part pins AM/PM («без пятнадцати три ночи» → 02:45). ✅
3. Gates green; 100% coverage. ✅

## Files
- `services/api/app/calendar/service_resolver.py` (`_BEFORE_MINUTES`, `_BEFORE_RE`, `before` group in `_scan_clocks`)
- tests: `test_calendar_service_resolver.py`
