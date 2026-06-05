# Story 12.81: Defer a trip-duration FAQ as a question, not a booking (round-20 R20-2)

Status: review

## Story
As a customer who asks «сколько по времени длится поездка?», I want the question routed to a human (or answered), not treated as a booking with a «на какую дату?» reply.

## Root cause (CONFIRMED)
«сколько длится поездка?» is `is_sales_intent=True` and `classify=other` → booking funnel → «на какую дату планируете?». No FAQ branch. The configured `duration_minutes` is the slot length used for availability, which may not be the real trip duration, so we defer rather than risk a wrong number (per the round-20 decision).

## Fix
`is_duration_question` («сколько … длится/по времени/продолжительность», «как долго») dispatches before the funnel to `_handle_faq_defer`: a short ack («Уточню у коллег и сразу сообщу.») + a HITL ticket carrying the verbatim question (`reason='sales_faq'`) — a question-style defer, never a booking handoff or a date/time ask. Shared with the working-hours-no-config case.

## Acceptance Criteria
1. «сколько по времени длится поездка?» / «как долго катаемся?» → a defer ack + HITL question, never a date/time ask or booking handoff. ✅
2. «сколько стоит?» (price) / «до скольки работаете?» (hours) are NOT mistaken for duration. ✅
3. Deterministic (no LLM). ✅
4. Gates green; 100% coverage. ✅

## Files
- `services/api/app/sales/sales_persona_answerer.py` (`is_duration_question`, `FAQ_DEFER_LINE`, `HITL_REASON_FAQ`, `_handle_faq_defer`, dispatch branch)
- tests: `test_sales_persona_answerer_early_busy_check.py`
