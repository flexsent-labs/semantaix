# Story 12.59: A capacity question is answered, not thanked-and-handed-off (round-14)

Status: review

## Story
As a customer asking «сколько багги нужно?», I want an answer (or an honest "I'm finding out"), not «Спасибо! Передам детали…» as if I'd completed a booking.

## Root cause
«Нас восемь человек, сколько багги нужно?» was treated as a booking turn → `SCOPING_COMPLETE_HANDOFF_LINE` («Спасибо! Передам детали…»). The capacity number is data-blocked (no per-vehicle capacity in the catalog).

## Fix
`is_capacity_question` (a «сколько … нужно/понадобится/вместит» question, distinct from a price ask «сколько стоит»). Routed early in `_dispatch` (any state) to `_handle_capacity_question`, which escalates to a human with question-appropriate copy `CAPACITY_ESCALATION_LINE` («Уточняю у коллег, какие варианты есть, и сразу сообщу.») — never the booking thank-you.

## Acceptance Criteria
1. A capacity question → «Уточняю у коллег…» + HITL escalation (reason `sales_capacity_question`); never «Спасибо! Передам детали…». ✅
2. A price ask / plain headcount is unaffected. ✅
3. Gates green; 100% coverage. ✅

## Note
The per-buggy capacity count is still data-blocked; this fixes the routing + copy (answer/escalate, don't thank). Configuring per-vehicle capacity remains an operator task.

## File List
- `services/api/app/sales/sales_persona_answerer.py` (`is_capacity_question`, `_handle_capacity_question`, `CAPACITY_ESCALATION_LINE`, `HITL_REASON_CAPACITY`, dispatch branch)
- `tests/test_sales_persona_answerer_early_busy_check.py`
