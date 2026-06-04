# Story 12.61: Vague-time window-propose fires on a complete booking too (round-15)

Status: review

## Story
As a customer who gives every detail except a fuzzy time («…во второй половине дня…»), I want the bot to propose a concrete slot in that window, so I'm not asked the bland «дату и время» when it already knows the date.

## Root cause (CONFIRMED)
The vague-window handler (12.60) was wired only into the greeting hook and the scoping *not-complete* branch. When the booking is already complete (round 15 had persistent thread state from round 14), the turn takes the completion path (`_complete_booking → _should_ask_for_time → _ask_for_time`), which never ran the window check — so a vague time fell to the generic `ASK_FOR_TIME_LINE`. Reproduced: a complete scoping state + «во второй половине дня» returned «Уточните дату и время», not the window offer.

## Fix
Thread the raw `question` into `_complete_booking` and run `_maybe_answer_vague_window` there before `_should_ask_for_time`. The handler no-ops (returns None) when there's no vague window or a concrete time is present, so concrete/normal bookings are unaffected. `question` defaults to None for legacy callers.

## Acceptance Criteria
1. A complete booking with a vague time → window-propose («Да, есть свободное время, например в HH:MM…»), not the generic ask. ✅
2. A concrete-time complete booking is unaffected (verdict/handoff as before). ✅
3. Greeting + mid-scoping vague paths unchanged. ✅
4. Gates green; 100% coverage. ✅

## Files
- `services/api/app/sales/sales_persona_answerer.py` (`_complete_booking` gains `question`; scoping + awaiting-time callers pass it)
- `tests/test_sales_persona_answerer_early_busy_check.py`
