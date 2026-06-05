# Story 12.85: Mixed-service request → one-at-a-time clarify (round-21 R21-1)

Status: review

## Story
As a customer who names two services in one message («двоих на багги и ещё двоих на квадроциклах»), I want the bot to handle them one at a time, not collapse both into a single ambiguous «свободно».

## Root cause (CONFIRMED)
A message naming two services + per-service counts wasn't distinguished — the funnel produced one verdict. Multi-intent family (R16-2 multi-date, R18-5 two-bookings, R20-4 price+availability). Note: calendar blocking is global per-day (R19-1 decision) and only one service is configured, so per-service verdicts would mostly echo the same занято/свободно — the real ambiguity is the two counts.

## Fix
`is_mixed_service_request` recognises ≥2 distinct vehicle/service types (small lexicon: багги, квадроцикл, эндуро, мотоцикл, скутер, …). In the greeting/scoping intercept chain (after the two-bookings hook) it returns a one-at-a-time clarification («Давайте оформим по одной услуге за раз. С какой начнём?»), no escalation, funnel intact — so the customer picks a service and the normal flow continues.

## Acceptance Criteria
1. «двоих на багги и двоих на квадроциклах» → a one-at-a-time clarify, not a single collapsed verdict. ✅
2. A single-service message («хочу багги», «сколько стоит квадроцикл?») is unaffected. ✅
3. Fires in greeting and mid-scoping; no escalation. ✅
4. Gates green; 100% coverage. ✅

## Files
- `services/api/app/sales/sales_persona_answerer.py` (`_VEHICLE_TYPE_GROUPS`, `is_mixed_service_request`, `MIXED_SERVICE_CLARIFY_LINE`, `_handle_mixed_service`, greeting + scoping intercepts)
- tests: `test_sales_persona_answerer_early_busy_check.py`

## Note (R21-3 — closing-boundary 18:00 is a confirmed NON-BUG)
The live Buggy23 config is **08:00–21:00** (not the QA's assumed "~08:00–18:00"), 60-min slots. So «завтра в 18:00» → ride 18:00–19:00 ends well within the 21:00 close → **correctly free**; «9 вечера»=21:00 → ride ends 22:00 > close → **correctly off-hours**. The last bookable START is already close − duration = 20:00 (`_fits_in_a_window` requires the ride to END by close). No code change — "fixing" it would break valid 18:00–20:00 starts. A boundary guard test (`test_closing_boundary_last_start_is_close_minus_duration`: 08:00/18:00/20:00 free, 20:01/21:00 off-hours at the real 21:00 close) locks the correct behavior. Validated against production data per the standing rule.

W3 (next-week weekday → 9 June) and W4 («9 вечера» → 21:00) positives also locked with guard tests.
