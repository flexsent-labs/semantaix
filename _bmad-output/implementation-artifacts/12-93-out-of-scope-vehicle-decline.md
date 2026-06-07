# Story 12.93: A non-offered vehicle/activity is declined, not handed off (round-27 R27-1)

Status: review

## Story
As a customer who asks «А на вертолёте полетать можно?» (a service the buggy persona doesn't offer), I want a clear "we only do buggies" reply, not «Передам коллегам для подтверждения…» (a booking handoff).

## Root cause (CONFIRMED)
Reproduced against the deployed detector: `is_out_of_scope` only knew dining/lodging nouns (ресторан, отель, …). «вертолёт» (and other non-offered vehicles/activities) is not in the lemma set, so the turn fell through the funnel and was answered with the booking-acceptance handoff. Verified live: `is_out_of_scope("А на вертолёте у вас полетать можно?")` → False; `is_sales_intent(...)` → False (so the existing `not is_sales_intent`-gated decline branch fires once the lemma is recognised).

## Fix
Extend `_OUT_OF_SCOPE_LEMMAS` with the unambiguous non-offered modes — aircraft and watercraft: вертолёт/вертолет, самолёт/самолет, яхта, катер, лодка, параплан, парашют, дельтаплан. Conservative (no багги / квадроцикл / прокат words), so a real buggy booking is never swallowed. Quadbikes stay in-scope. The existing `_handle_out_of_scope` → `OUT_OF_SCOPE_DECLINE_LINE` («…я по прокату багги. Подскажу с поездкой: даты и сколько человек?») now covers it.

## Acceptance Criteria
1. «на вертолёте?» / «яхту арендовать?» → the out-of-scope decline, never a booking handoff. ✅
2. «на квадроцикле» (offered) and a real buggy booking are NOT declined. ✅
3. Deterministic (lemma-based, no LLM); funnel state untouched (no upsert). ✅
4. Gates green; 100% coverage. ✅

## Files
- `services/api/app/sales/out_of_scope.py` (`_OUT_OF_SCOPE_LEMMAS` + docstring)
- tests: `test_sales_persona_answerer_out_of_scope.py`
