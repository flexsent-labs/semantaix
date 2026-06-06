# Story 12.92: A pure greeting gets a greeting, not a handoff (round-26 R26-3)

Status: review

## Story
As a customer who just says «Здравствуйте!», I want a courteous greeting + booking prompt, not «Спасибо! Передам детали коллегам на подтверждение…».

## Root cause (CONFIRMED)
Reproduced: «Здравствуйте!» parked in **pitching** → `_handle_pitching` treats it as neither a counter-offer, nor an acceptance, nor a closure → `_handoff_after_pitching_followup` → booking handoff. Same over-eager-handoff family as R16-4 (gratitude) / R18-6 (gibberish).

## Fix
`is_pure_greeting` (the leading-greeting grammar, True only when nothing substantive remains after stripping the salutation) dispatches before the funnel — in ANY stage — to `_handle_greeting_smalltalk` → «Здравствуйте! На какую дату хотите записаться?», no escalation, funnel intact. No `not is_sales_intent` gate is needed: `is_pure_greeting` is already False for «здравствуйте, хочу багги» (booking content remains), so a greeting+booking still books.

## Acceptance Criteria
1. «Здравствуйте!» (any stage, incl. pitching) → a courteous greeting + prompt, never a booking handoff. ✅
2. «здравствуйте, хочу багги завтра» still books (not a pure greeting). ✅
3. Deterministic (no LLM); funnel state left intact. ✅
4. Gates green; 100% coverage. ✅

## Files
- `services/api/app/sales/sales_persona_answerer.py` (`is_pure_greeting`, `GREETING_SMALLTALK_LINE`, `_handle_greeting_smalltalk`, dispatch branch)
- tests: `test_sales_persona_answerer_early_busy_check.py`

## Note (B4 — far-future «15 января» defers)
Deferred this round. The round-17 effective-lookahead extends the freeBusy query to cover a far date (~221 days for «15 января»), which the live Google freeBusy API likely rejects → STATUS_ERROR → the unverified-slot defer («Проверю это время»). Defensible per the QA; aligning bare far-dates with the explicit-year «14 июня 2027» → свободно would require capping/handling the freeBusy range — its own scoped change. R23-1 price / N2 capacity remain data-blocked.
