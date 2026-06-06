# Story 12.86: A multi-turn time change re-runs the availability check (round-23 R23-3)

Status: review

## Story
As a customer who confirms a slot and then changes the time on a later turn («…9 июня в 14:00» → «давайте лучше в 12:00 в тот же день»), I want the bot to re-check the new time, not hand off toward a slot that's actually busy.

## Root cause (CONFIRMED)
After a FREE handoff the chat parks in `pitching` with `last_proposal=None` (no alternative was offered — the customer's own time was free). A later «лучше в 12:00 в тот же день» carries no full date, so `extract_requested_start` → `None` (reproduced) → the date counter-offer (a) doesn't fire; and the time-only counter-offer (a2) needs an offered slot (`last_proposal`), which is `None` → it doesn't fire either. So the turn fell to the generic handoff with NO re-check — the cross-turn analogue of R17-2 (in-message self-correction).

## Fix
A new branch (a3) in `_handle_pitching`: when there's no offered slot but `intent.dates` holds the customer's prior slot, parse that prior date+time, and if the reply names exactly one NEW clock time, re-resolve `prior_date + new_time` and re-run `_complete_booking` (which re-checks the calendar). «9 июня 14:00 (свободно)» → «лучше в 12:00» → 9 June 12:00 ∈ 11–13 → занято + nearest-free alternative.

## Acceptance Criteria
1. «9 июня 14:00» (free, parked) → «лучше в 12:00 в тот же день» → re-checked → занято + alternative. ✅
2. A non-time reply after a free handoff still hands off (no re-check, no crash). ✅
3. Carries the unchanged date; only fires for a single new time (mirrors R17-2 last-wins). ✅
4. Gates green; 100% coverage. ✅

## Files
- `services/api/app/sales/sales_persona_answerer.py` (`_handle_pitching` branch a3)
- tests: `test_sales_persona_answerer_early_busy_check.py`
