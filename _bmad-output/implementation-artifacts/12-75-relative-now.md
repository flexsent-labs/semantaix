# Story 12.75: Resolve «прямо сейчас» / «сейчас» → now (round-18 R18-2)

Status: review

## Story
As a customer who asks to ride «прямо сейчас», I want the bot to check the current time against the calendar (→ off-hours at night, or a verdict) instead of asking me to specify a time.

## Root cause (CONFIRMED)
There was no «сейчас» grammar; `extract_requested_start("прямо сейчас")` → `None` → the slot-fill flow asked for the time. Same relative-time family as R17-3 («через N часов»).

## Fix
`_extract_relative_offset` (the round-17 «через» resolver) now also returns `local_now` for «сейчас» / «прямо сейчас» / «прям сейчас», excluding the «не сейчас» deferral. The resolved instant flows through the normal availability check (e.g. 23:5x → off-hours).

## Acceptance Criteria
1. «прямо сейчас» / «сейчас» → the current instant (→ off-hours / verdict). ✅
2. «не сейчас» (a deferral) does not resolve to now. ✅
3. Gates green; 100% coverage. ✅

## Files
- `services/api/app/calendar/service_resolver.py` (`_NOW_RE`, `_NOT_NOW_RE`, in `_extract_relative_offset`)
- tests: `test_calendar_service_resolver.py`
