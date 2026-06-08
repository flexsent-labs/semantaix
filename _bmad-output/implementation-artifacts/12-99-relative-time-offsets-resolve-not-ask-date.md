# Story 12.99: «Через два часа» and «прямо сейчас» resolve to a datetime, not ask for date (round-28 D2)

Status: review

## Story
As a customer who says «Хочу забронировать багги через два часа, нас двое», I want the bot to check availability at now+2h and return a verdict — not ask «Уточните, пожалуйста, на какую дату записать?».

## Root cause (CONFIRMED — live regression, 2026-06-08)

`_has_time_without_date` (lines 2902–2929) scans raw `question` for clocks via `extract_all_clocks`. For «через два часа, нас двое», the word-form clock extractor matches «два» (word-form for digit 2) and returns `[(2, 0)]` — treating it as 02:00. Since `extract_requested_date("через два часа")` cannot resolve a concrete calendar date from a relative-duration phrase alone, it returns `None`. Condition: clock found AND no date → `_has_time_without_date = True` → asks for date.

For «прямо сейчас»: `extract_all_clocks` returns no clock (no hour digit or word), but `extract_requested_start("прямо сейчас", now, tz)` should resolve to `now`. However `_has_time_without_date` doesn't call `extract_requested_start` on the raw question for the "does a complete datetime exist?" check. If `extract_requested_date("прямо сейчас")` returns `None` AND `intent.dates` is also unresolvable, the bot asks for a date.

The bug was introduced in story 12-96 (raw-text fallback) which correctly helps «в 16:00» (bare clock, no date) but over-fires on relative expressions that already encode a full instant.

Verified live (2026-06-08):
- «Хочу забронировать багги через два часа, нас двое» → «Уточните, пожалуйста, на какую дату записать?» ❌
- «Хочу забронировать багги прямо сейчас, двоих» → «Уточните, пожалуйста, на какую дату записать?» ❌

## Fix

At the top of `_has_time_without_date`, short-circuit to `False` when the question already contains a complete resolvable datetime via `extract_requested_start`:

```python
async def _has_time_without_date(self, *, ctx, intent, question=None):
    cal = await self._calendar_booking_context(ctx=ctx)
    if cal is None:
        return False
    # Short-circuit: if the raw question resolves to a full start datetime
    # (handles relative offsets like «через два часа», «прямо сейчас»,
    #  «через 3 дня в 14:00»), there is no missing date — the offset IS the slot.
    if question:
        full_start = extract_requested_start(
            text=question, now=ctx.now, project_tz=cal.project_tz
        )
        if full_start is not None:
            return False
    # ... existing clock + date scan ...
```

This means: if `extract_requested_start` can resolve a concrete start from the raw question (which it already does for relative offsets per stories 12-70 and 12-75), the time-without-date check is skipped. The booking proceeds to the calendar check with the resolved slot.

## Acceptance Criteria
1. «Хочу через два часа на багги, нас двое» → availability verdict (free/busy/off-hours), never date-ask. ✅
2. «Прямо сейчас, двоих» → availability verdict at current instant. ✅
3. «Через 3 дня в 14:00, нас двое» → verdict for that slot. ✅
4. «В 16:00 на багги» (bare clock, no date) → still asks for date (existing 12-91/12-96 path unchanged). ✅
5. Gates green; 100% coverage.

## Files
- `services/api/app/sales/sales_persona_answerer.py` (`_has_time_without_date` short-circuit)
- tests: `test_sales_persona_answerer_early_busy_check.py`
