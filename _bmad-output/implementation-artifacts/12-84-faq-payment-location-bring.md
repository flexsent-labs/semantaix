# Story 12.84: Payment / location / what-to-bring FAQs defer, not booking (round-21 R21-2)

Status: review

## Story
As a customer who asks «оплатить картой можно?» or «где вы находитесь?», I want the question handled (or routed to a human), not turned into a booking-completion handoff.

## Root cause (CONFIRMED)
Payment / location / what-to-bring messages are `is_sales_intent=False`, so mid-funnel (a long live session) they fell through to the booking-handoff path. They extend the FAQ cluster (R20-1 hours, R20-2 duration) and the over-eager-handoff root cause (R16-3/R16-4/R18-6). There's no payment/location data in config, so they defer.

## Fix
`is_info_faq_question` (payment: «оплат…/наличны…/картой…»; location: «где вы…/как добраться/адрес…»; what-to-bring: «что взять с собой…») dispatches before the funnel (alongside the duration FAQ) to `_handle_faq_defer`: a short ack + HITL ticket carrying the verbatim question (`reason='sales_faq'`) — a question-style defer, never a booking handoff or date/time ask. Gated on not a booking-commit so «запишите …» still books.

## Acceptance Criteria
1. «оплатить картой можно?» / «где вы находитесь, как добраться?» / «что взять с собой?» → a defer ack + HITL question, never a booking handoff. ✅
2. «сколько стоит?» / «свободно ли завтра в 12:00?» / a real booking are NOT mistaken for an info FAQ. ✅
3. Deterministic (no LLM). ✅
4. Gates green; 100% coverage. ✅

## Files
- `services/api/app/sales/sales_persona_answerer.py` (`_PAYMENT_RE`, `_LOCATION_RE`, `_BRING_RE`, `is_info_faq_question`, dispatch branch → `_handle_faq_defer`)
- tests: `test_sales_persona_answerer_early_busy_check.py`
