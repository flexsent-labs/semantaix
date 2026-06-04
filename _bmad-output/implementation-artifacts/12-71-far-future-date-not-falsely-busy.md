# Story 12.71: A far-future free date isn't falsely "busy" + one template per turn (round-17 R17-4)

Status: review

## Story
As a customer who names a date far ahead («15 декабря в 14:00»), I want to be told it's **free** when nothing is booked — not falsely "занято", and not with two contradictory sentences glued together.

## Root cause (CONFIRMED)
Two distinct bugs produced «К сожалению, это время уже занято. Спасибо! Передам детали коллегам на подтверждение…»:
1. **False busy.** `compute_availability` rejects any slot `>= now + lookahead_days` with `REASON_OUTSIDE_LOOKAHEAD` (`availability.py:238-240`), and `check_requested_availability` capped its freebusy query at `now + lookahead_days` too. `lookahead_days` defaults to **60**; «15 декабря» is ≈194 days out → `UNAVAILABLE(outside_lookahead)`, and `find_earliest_slot`'s window `[Dec 15, now+60d]` was empty → `alternative=None`.
2. **Template merge.** `_UNAVAILABLE_LEAD_LINES` had no entry for `outside_lookahead` → it fell back to `SLOT_BUSY_LINE` ("занято"); then `_propose_alternative_or_handoff` with `alternative=None` built `SLOT_BUSY_LINE + " " + SCOPING_COMPLETE_HANDOFF_LINE` — pairing the busy verdict with the booking-confirmation handoff, which can't both be true.

## Fix
1. **Verify the named date.** `check_requested_availability` widens the effective lookahead to cover the customer's named date (`max(lookahead_days, min(days_to_requested + 1, 400))`) and uses it for the freebusy window, the parsed rule's horizon, and the alternative-scan window. The cap (400 days) keeps the backend query bounded; beyond it the slot stays `outside_lookahead`. The generic lookahead still bounds the "find me a slot" scan — this only changes an explicit "is THIS date free?" check.
2. **Honest copy, one template.** `outside_lookahead` now maps to `SLOT_TOO_FAR_LINE` («…так далеко вперёд бронирование пока недоступно.»), not "занято". The no-alternative branch appends a single coherent escalation clause (`BUSY_NO_SLOT_HANDOFF_TAIL` — «Передам ваш запрос коллеге - подберут время.»), never the contradictory booking-confirmation handoff. A human still picks up (the turn escalates).

## Acceptance Criteria
1. A future date with no events → free (свободно); the freebusy window covers the requested instant; year resolves to the next occurrence (bare «15 декабря» → 2026). ✅
2. A far-future date that IS busy → unavailable + a same-day alternative. ✅
3. Beyond the 400-day cap → honest "too far ahead", never a false "занято". ✅
4. Exactly one response template per turn — busy/unavailable + no slot is one coherent message (verdict + colleague-will-find-a-time), never busy-verdict + booking handoff. ✅
5. Gates green; 100% coverage. ✅

## Files
- `services/api/app/calendar/requested_time_check.py` (`_REQUESTED_LOOKAHEAD_CAP_DAYS`, effective-lookahead window/horizon)
- `services/api/app/sales/sales_persona_answerer.py` (`SLOT_TOO_FAR_LINE`, `BUSY_NO_SLOT_HANDOFF_TAIL`, `outside_lookahead` mapping, `_propose_alternative_or_handoff` no-merge)
- tests: `test_requested_time_check.py`, `test_sales_persona_answerer_early_busy_check.py`, `test_sales_persona_answerer_pitching.py`, `test_sales_persona_answerer_propose_alternative_defers_hitl.py`
