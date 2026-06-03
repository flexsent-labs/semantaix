# Story 12.60: A vague time window proposes a concrete slot (round-14 R14-1)

Status: review

## Story
As a customer naming a fuzzy time («завтра во второй половине дня»), I want the bot to check that window and propose a concrete slot, not reject my in-scope booking with «Не смогу тут помочь.».

## Root cause
«Хотим завтра … во второй половине дня …» (in-scope, `is_sales_intent=True`) reached the greeting LLM, which DECLINED an in-scope booking because the time was vague. The persona had no deterministic vague-time handling, so it depended on the (unreliable) LLM, and the scope guard's `_is_in_scope` would also mis-classify a vague-time booking (no concrete time parses).

## Fix (per the user's refinement: check the window, then propose a slot)
`detect_vague_window` maps a phrase to an `(start, end)` hour window (утром 8–12, до обеда 8–12, во второй половине дня / после обеда 12–18, обед 12–15, вечером 16–20, днём 12–18). `_maybe_answer_vague_window` (run before the busy-intercept in greeting + scoping, only when no concrete clock is present) extracts the date (new `extract_requested_date`), checks the window-start slot via `check_requested_availability`, and proposes a concrete FREE slot (the window start if free, else the calendar's nearest-free) with `VAGUE_WINDOW_OFFER_LINE` («Да, есть свободное время, например в 12:00. Подойдёт или назовите удобное время?»). It parks in **pitching with the proposed slot**, so the follow-up («да» / a concrete counter-time) is handled by the existing accept/counter machinery (Story 12.51), with the date carried from the offer. No decline, no escalation on the offer turn.

## Acceptance Criteria
1. A vague-time in-scope booking → a clarifying offer with a concrete free slot, never «Не смогу тут помочь.». ✅
2. Windows map sensibly (morning/afternoon/evening), clamped by the calendar. ✅
3. The follow-up (accept or a concrete time) runs the normal availability check + books, date carried from the offer. ✅
4. A concrete time is unaffected (falls through to the normal flow). ✅
5. Gates green; 100% coverage. ✅

## File List
- `services/api/app/calendar/service_resolver.py` (`extract_requested_date`)
- `services/api/app/sales/sales_persona_answerer.py` (`detect_vague_window`, `_maybe_answer_vague_window`, `VAGUE_WINDOW_OFFER_LINE`, hooks)
- `tests/test_sales_persona_answerer_early_busy_check.py`, `tests/test_calendar_service_resolver.py`
