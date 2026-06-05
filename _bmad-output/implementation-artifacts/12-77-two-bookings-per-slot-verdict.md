# Story 12.77: Two bookings in one message → per-slot verdict (round-18 R18-5)

Status: review

## Story
As a customer who names two slots in one message («завтра в 12:00 и послезавтра в 15:00»), I want a verdict for **both**, not one silent «свободно» that drops the second slot.

## Root cause (CONFIRMED)
Round-16's multi-date handler is gated on «или» (one shared clock, two day options) and uses `clocks[0]` for both. A «… и …» message with a distinct time per slot didn't match, so the funnel handled only the first slot and dropped the second.

## Fix
`_maybe_answer_two_bookings` splits the turn on a standalone «и» and parses each part with `extract_requested_start` (each side must carry its OWN concrete date+time). When ≥2 distinct concrete bookings parse and the calendar verifies them, it returns a per-slot verdict: «30 мая в 12:00 - свободно; 31 мая в 15:00 - свободно. Подтвердить оба или выбрать один?». Strictly gated: a stray «и» («я и друг … в 12:00» — only one parseable booking) falls through, and it `None`s when the calendar can't verify both. Wired into the greeting and scoping intercepts, after the «или» multi-date handler.

## Acceptance Criteria
1. «завтра в 12:00 и послезавтра в 15:00» → both slots named with their own verdict. ✅
2. One busy + one free → «занято; свободно». ✅
3. A single booking with a stray «и» is NOT split into two. ✅
4. Calendar disabled / unverifiable → falls through to the normal flow. ✅
5. Gates green; 100% coverage. ✅

## Files
- `services/api/app/sales/sales_persona_answerer.py` (`_maybe_answer_two_bookings`, wired in greeting + scoping)
- tests: `test_sales_persona_answerer_early_busy_check.py`
