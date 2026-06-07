# Story 12.94: A discount question defers as an FAQ, not a booking handoff (round-27 R27-2)

Status: review

## Story
As a customer who asks «А у вас скидки для группы есть?», I want an informational defer («Уточню у коллег и сразу сообщу»), not «Передам детали коллегам на подтверждение…» (booking-completion copy).

## Root cause (CONFIRMED)
Reproduced against the deployed FAQ cluster: `is_info_faq_question` covered payment / location / what-to-bring (and hours/days/duration have their own branches), but not **discounts / promos**. «скидки» fell through the funnel → booking handoff. Verified live: `is_info_faq_question("А у вас скидки для группы есть?")` → False (pre-fix).

## Fix
Add `_DISCOUNT_RE` (скидк*, скидок, акци*, промокод*, спецпредложен*, подешевл*, дешевл*, «по скидк*») and include it in `is_info_faq_question`. A discount question now routes to the existing `_handle_faq_defer` → `FAQ_DEFER_LINE` («Уточню у коллег и сразу сообщу.») with an HITL escalation tagged `sales_faq` — the same defer-to-a-human pattern as payment/location (no invented discount policy; the catalog has no discount data). Per the user's round-27 decision (defer to a human).

## Acceptance Criteria
1. «скидки/акции/промокод/подешевле?» → the FAQ defer, escalate=True, hitl_reason=sales_faq, never the booking handoff. ✅
2. A real booking («хочу записаться на багги») is NOT matched. ✅
3. Deterministic (no LLM); no funnel state created. ✅
4. Gates green; 100% coverage. ✅

## Files
- `services/api/app/sales/sales_persona_answerer.py` (`_DISCOUNT_RE`, `is_info_faq_question`)
- tests: `test_sales_persona_answerer_early_busy_check.py`

## Note (interaction with the escalation-coalesce test)
`test_sales_escalation_coalesce.py` used «А скидка для группы есть?» as its second inbound — which now correctly routes to the FAQ-defer path (sales_faq) rather than the pricing fallback. The test verifies *coalescing of pricing escalations*, so its follow-up was changed to a genuine price question («А за 8 часов сколько будет стоить?»). Discount→FAQ-defer is covered by the new R27-2 tests.
