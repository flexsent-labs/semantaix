# Story 12.98: Out-of-scope pure-service requests with «хочу» still decline cleanly (round-28 D1)

Status: review

## Story
As a customer who says «Хочу на вертолёте полетать», I want a clean out-of-scope decline («Этим, к сожалению, не помогу - я по прокату багги»), not a mixed response that first asks «На какую дату планируете полёт?» then appends the decline.

## Root cause (CONFIRMED — live regression, 2026-06-08)

Dispatch condition at line 1403-1406:
```python
if is_out_of_scope(question) and not is_sales_intent(question):
    return self._handle_out_of_scope(ctx=ctx)
```

The gate `not is_sales_intent` is intentional for MIXED messages like «хочу багги и ресторан» — those should stay in the funnel with a mixed-service suffix. But it fires incorrectly when the message contains ONLY a non-offered service (no offered service at all). «Хочу на вертолёте полетать» / «Хочу в ресторан сходить» trigger `is_sales_intent=True` because «хочу» + a booking verb matches the sales-intent classifier, even though there is no offered service in the message. This routes both cases to the mixed-intent path which asks for a date (for the non-existent booking) then appends «А с остальным, не помогу».

Verified live (2026-06-08):
- «Хочу на вертолёте полетать» → «На какую дату планируете полёт?\nА с остальным, к сожалению, не помогу - я по прокату багги.» ❌
- «Можно на яхте покататься?» (no «хочу») → «Этим, к сожалению, не помогу - я по прокату багги.» ✅

## Fix

Introduce `_mentions_offered_service(question: str) -> bool` (lemma or regex scan for offered service names: багги, квадроцикл, эндуро, мотоцикл, скутер). Update the dispatch condition:

```python
if is_out_of_scope(question) and (
    not is_sales_intent(question)
    or not _mentions_offered_service(question)
):
    return self._handle_out_of_scope(ctx=ctx)
```

When the message is out-of-scope AND mentions no offered service, decline purely — regardless of whether «хочу» triggered `is_sales_intent`. The mixed-intent path (suffix) is only reached when an offered service IS also present.

## Acceptance Criteria
1. «Хочу на вертолёте полетать» → clean `OUT_OF_SCOPE_DECLINE_LINE`; no date-ask prefix. ✅
2. «Хочу в ресторан сходить» → clean `OUT_OF_SCOPE_DECLINE_LINE`. ✅
3. «Хочу багги и ресторан» → mixed-intent path (date-ask + decline suffix) still fires (offered service present). ✅
4. «Можно на яхте?» → unchanged clean decline. ✅
5. Gates green; 100% coverage.

## Files
- `services/api/app/sales/sales_persona_answerer.py` (dispatch condition, new `_mentions_offered_service`)
- tests: `test_sales_persona_answerer_out_of_scope.py` / `test_sales_persona_answerer_early_busy_check.py`
