# Story 12.89: Flag a contradictory vehicles>people count (round-25 R25-1)

Status: review

## Story
As a customer who asks for more buggies than there are people («нас двое, но хотим сразу 5 багги»), I want the bot to flag the mismatch and clarify, not silently confirm «свободно».

## Root cause (CONFIRMED)
The funnel accepted any `headcount` + `vehicle_count` and went straight to the slot verdict. 5 buggies for 2 people is implausible (more vehicles than riders) but was never questioned. This is a logical-consistency gap adjacent to N2 — but the vehicles>people direction needs **no** per-vehicle capacity data (unlike N2's people≫capacity direction).

## Fix
`is_count_inconsistent(intent)` is True when both counts parse to positive ints (`_as_positive_int` — rejecting bools/non-digits/non-positive) and `vehicle_count > headcount`. An intercept in the greeting + scoping flow (before the availability check) returns `_handle_count_mismatch` → «Вы указали 5 багги на 2 - обычно нужно меньше. Сколько багги оформить?», no escalation, funnel intact — so the customer corrects the count and the normal flow continues.

## Acceptance Criteria
1. «двое, но 5 багги» → a clarify before confirming, never a silent «свободно». ✅
2. Plausible counts (1 buggy/2 people, 2/2) are unaffected. ✅
3. Fires in greeting and mid-scoping; no escalation. ✅
4. Gates green; 100% coverage. ✅

## Files
- `services/api/app/sales/sales_persona_answerer.py` (`is_count_inconsistent`, `_as_positive_int`, `COUNT_MISMATCH_CLARIFY_LINE`, `_handle_count_mismatch`, greeting + scoping intercepts)
- tests: `test_sales_persona_answerer_early_busy_check.py`

## Notes
- The opposite direction (people ≫ per-buggy capacity, e.g. 10 people / 1 buggy) still needs the real seats-per-buggy figure — **N2**, data-blocked.
- **A1/A2 positives** locked with parse guards: dotted «09.06 в 12:00» → 9 June 12:00; «ровно в 8:00» → 08:00 (opening boundary bookable).
- **R23-1 price** remains data-blocked (no readable price); **R24-1** stayed fixed (verified live this round).
