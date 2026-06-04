# Story 12.69: In-message self-correction — last value wins (round-17 R17-2)

Status: review

## Story
As a customer who corrects myself in one message («…в 14:00… хотя нет, лучше в 12:00»), I want the bot to use my **last** stated time, so the availability verdict reflects what I actually want — not the first thing I said.

## Root cause (CONFIRMED)
`_extract_clock` used `re.search()`, which returns the **first** match. For «9 июня в 14:00, хотя нет, лучше в 12:00» it committed to 14:00 (free) and ignored the corrected 12:00 (busy → should be занято). The first value always won.

## Fix
`_extract_clock` now scans **all** clocks via the shared `_scan_clocks` (returned in text order) and takes the **last** one. A later time in the same message therefore overrides an earlier one. Because the scanner also recognises word-form times (Story 12.68), the override works for both digit («14:00 → 12:00») and word («два часа дня → четыре часа дня») corrections. A single-time message is unaffected (the only clock is also the last).

## Acceptance Criteria
1. «9 июня в 14:00, хотя нет, лучше в 12:00» → 9 June 12:00 (the corrected, busy slot). ✅
2. Word-form correction «два часа дня… лучше четыре часа дня» → 16:00. ✅
3. A single-time message resolves to that time (no regression). ✅
4. Gates green; 100% coverage. ✅

## Files
- `services/api/app/calendar/service_resolver.py` (`_extract_clock` last-wins via `_scan_clocks`)
- tests: `test_calendar_service_resolver.py`
