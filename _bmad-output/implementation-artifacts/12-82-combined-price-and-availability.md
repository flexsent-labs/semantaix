# Story 12.82: A combined price + availability message answers both (round-20 R20-4)

Status: review

## Story
As a customer who asks «Сколько стоит и свободно ли завтра в 12:00?», I want BOTH the availability verdict (занято/свободно) and the price reply in the same turn — not just the price with the verdict silently dropped.

## Root cause (CONFIRMED)
The message is `classify=price_ask` AND `is_availability_inquiry=True`. Whichever single-intent handler claimed the turn dropped the other: in a pricing state the price path won (verdict dropped, the live symptom «Уточню у коллег…»); on a first turn the availability-inquiry handler won (price dropped). The two intents were never combined.

## Fix
`_maybe_answer_price_and_availability` dispatches before the stage routing: when the turn is a price ask AND a concrete slot parses AND the calendar verifies it, it builds the verdict (via the shared `_availability_verdict_text`, also used by the availability-inquiry handler) and runs the existing `_handle_pricing`, returning «{verdict} {price reply}» in one turn. It returns `None` (falling through to the single-intent paths) when pricing isn't configured, there's no concrete slot, the slot is unverifiable, or pricing errors — so neither half is ever a bogus answer.

## Acceptance Criteria
1. «Сколько стоит и свободно ли завтра в 12:00?» (busy) → «…занято…» + the price reply (here a defer), in one turn. ✅
2. A price ask with no concrete slot → price-only (combined doesn't fire). ✅
3. Unverifiable slot / pricing error → fall through, no half-answer. ✅
4. The availability check is never skipped because a price intent is present. ✅
5. Gates green; 100% coverage. ✅

## Files
- `services/api/app/sales/sales_persona_answerer.py` (`_availability_verdict_text`, `_maybe_answer_price_and_availability`, dispatch branch; `_maybe_answer_availability_inquiry` refactored to share the verdict helper)
- tests: `test_sales_persona_answerer_early_busy_check.py`
