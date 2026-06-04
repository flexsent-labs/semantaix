# Story 12.70: Resolve relative time offsets «через N часов» (round-17 R17-3)

Status: review

## Story
As a customer who asks for a slot relative to now («можно через два часа?», «через 30 минут», «через 3 дня»), I want the bot to work out the concrete datetime and check the calendar, instead of dropping me into a generic handoff with no verdict.

## Root cause (CONFIRMED)
There was no «через …» grammar anywhere; `extract_requested_start`'s docstring explicitly declined "fuzzy offsets like 'через час'" by design. So «через два часа» had no day anchor and no digit clock → `None` → generic handoff, no availability check ran.

## Fix
`_extract_relative_offset(text, local_now)` parses «через <N> <unit>» against "now" (Moscow). The count is a digit, a number word (`_WORD_NUMBERS`), or «пол» (half); a missing count means one. Units: час\* (hours), минут\* (minutes), дн\*/день (days), недел\* (weeks). Hours/minutes give a full instant (`now + delta`); a day/week offset keeps now's time-of-day unless the text also names an explicit clock («через 3 дня в 14:00»). `extract_requested_start` checks this offset **first** and returns it (it carries both date and time, so it wins over the day-anchor + clock path). Off-hours/past slots are handled downstream by the normal availability check.

## Acceptance Criteria
1. «через два часа» / «через час» / «через 30 минут» / «через полчаса» → now + the offset. ✅
2. «через 3 дня» / «через день» / «через неделю» → the future day at now's time; «через 3 дня в 14:00» honours the named clock. ✅
3. A non-time «через …» («через дорогу») or an unrecognised count («через сто часов») declines (returns `None`). ✅
4. Gates green; 100% coverage. ✅

## Files
- `services/api/app/calendar/service_resolver.py` (`_THROUGH_RE`, `_extract_relative_offset`, wired into `extract_requested_start`)
- tests: `test_calendar_service_resolver.py`
