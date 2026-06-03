# Story 12.58: An availability inquiry gets a verdict, not a HITL ticket (round-14)

Status: review

## Story
As a customer ASKING whether a slot is free («…в 16:30 свободно?»), I want a plain text verdict, not a booking handoff and a HITL ticket.

## Root cause
Any concrete-time turn was treated as a booking → free slot → `SLOT_FREE_HANDOFF_LINE` («передам коллегам») + HITL escalation. No distinction between an inquiry and a request.

## Fix
`is_availability_inquiry` = matches «свободн/занят/доступн/есть ли» AND no booking-commit verb. `_maybe_answer_availability_inquiry` (run before the busy-intercept in greeting + scoping) checks the slot and returns a verdict only — `SLOT_FREE_INQUIRY_LINE` («Да, это время свободно.») when free, the busy/off-hours lead line + nearest-free tail when not — with **no escalation, no HITL, no funnel mutation, suppress_followup**. A booking REQUEST still escalates as before.

## Acceptance Criteria
1. «…свободно?» free → «Да, это время свободно.», no ticket. ✅
2. «…свободно?» busy → «занято» + nearest free, no ticket. ✅
3. A booking request («запишите…») still handed off/escalated. ✅
4. Gates green; 100% coverage. ✅

## File List
- `services/api/app/sales/sales_persona_answerer.py` (`is_availability_inquiry`, `_maybe_answer_availability_inquiry`, `SLOT_FREE_INQUIRY_LINE`, `_format_alternative_tail`, hooks)
- `tests/test_sales_persona_answerer_early_busy_check.py`
