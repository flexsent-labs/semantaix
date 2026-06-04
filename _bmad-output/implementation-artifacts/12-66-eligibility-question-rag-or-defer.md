# Story 12.66: Eligibility/policy questions answered as questions (round-16 R16-3)

Status: review

## Story
As a customer asking «можно кататься с ребёнком 5 лет?» / «нужны ли права?», I want a policy answer (or an honest "I'll check"), not a booking confirmation.

## Root cause (CONFIRMED)
An eligibility question is `is_sales_intent=True` («багги/кататься») → booking funnel → handoff. It's a condition-of-use QUESTION, not a booking.

## Fix
`is_eligibility_question` — a distinctive eligibility noun (ребён/дет/возраст/права/беремен/собак/животн/новичк/опыт/вес/инвалид) PLUS a permission/question marker (можно/нужн/допуск/ли/со скольки), and no booking-commit verb. Dispatched before the funnel to `_answer_concept_via_rag` (the existing RAG-grounded path): a confident catalog chunk → a grounded answer; otherwise `handled=False` → the inbound defers to a human. **Verified the live catalog has no eligibility policy text**, so today it defers (no fabricated policy) and auto-answers once policy is added.

## Acceptance Criteria
1. Eligibility questions are handled as questions (RAG-grounded or deferred), never a booking handoff. ✅
2. Normal bookings / availability / capacity asks are unaffected. ✅
3. Grounded answer when a confident policy chunk exists. ✅
4. Gates green; 100% coverage. ✅

## Files
- `services/api/app/sales/sales_persona_answerer.py` (`is_eligibility_question` + dispatch branch → `_answer_concept_via_rag`)
- tests: `test_sales_persona_answerer_early_busy_check.py`
