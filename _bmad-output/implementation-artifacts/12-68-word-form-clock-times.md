# Story 12.68: Parse word-form / colloquial precise times (round-17 R17-1)

Status: review

## Story
As a customer who names a time in words («в полдень», «в три часа дня», «в девять утра», «в семь вечера»), I want the bot to understand it as a concrete time and run the normal availability check, instead of asking me to "specify the time" until I type digits.

## Root cause (CONFIRMED)
`_HH_CLOCK` in `service_resolver.py` was `\b(\d{1,2})\s*час…` — it required a **digit** hour. `_extract_clock` had no grammar for word-number hours («три»→3), the noon/midnight words («полдень»/«полночь»), or the part-of-day qualifier («утра»/«дня»/«вечера»/«ночи») that promotes a 1-12 hour to 24h. So «в три часа дня» and «в полдень» yielded no clock → `extract_requested_start` returned `None` → the funnel asked «Уточните… желаемое время». Only a numeric «15:00» was accepted. Distinct from R14-1's genuinely **vague** windows («во второй половине дня»), which correctly clarify.

## Fix
A unified clock scanner (`_scan_clocks`) replaces the digit-only path and recognises, in priority order: am/pm, the noon/midnight words, the word-or-digit "N часов [дня]" form, the bare "N <part-of-day>" form, and explicit "HH:MM". `_WORD_NUMBERS` maps Russian cardinals 1-12; `_apply_daypart` promotes the hour ("утра"/"ночи" keep it, 12→0; "дня"/"вечера" add 12, 12 stays noon). Word-form times now flow through the same availability check as digits.

## Acceptance Criteria
1. «в полдень»→12:00, «в полночь»→00:00, «в три часа дня»→15:00, «в девять утра»→09:00, «в семь вечера»→19:00, «в час дня»→13:00, «двенадцать ночи»→00:00. ✅
2. A bare word-hour with no part-of-day («в три часа») mirrors the digit «в 3 часа» → 03:00. ✅
3. A bare cardinal that is a headcount («пять человек») or a lone «час» is NOT read as a clock. ✅
4. Distinct from R14-1: genuinely vague windows still clarify. ✅
5. Gates green; 100% coverage. ✅

## Files
- `services/api/app/calendar/service_resolver.py` (`_WORD_NUMBERS`, `_DAYPART`, `_CLOCK_CHAS_RE`, `_CLOCK_DAYPART_RE`, `_NOON_MIDNIGHT_RE`, `_apply_daypart`, `_scan_clocks`)
- tests: `test_calendar_service_resolver.py`
