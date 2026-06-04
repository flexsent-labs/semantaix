# Story 12.65: Gratitude gets a courteous ack, not a booking handoff (round-16 R16-4)

Status: review

## Story
As a customer saying «спасибо большое, вы очень помогли!», I want a courteous acknowledgment, not «передам коллегам для подтверждения».

## Root cause (CONFIRMED)
Pure gratitude is `is_sales_intent=False`; in a CLOSING stage it hits `_handle_closing` → `CLOSING_HANDOFF_LINE`. (Also `is_no_more` is True for «спасибо большое, вы очень помогли!», so it read as a closure.) The booking-handoff fallback over-applied to a non-booking input.

## Fix
`is_gratitude` — a thanks word («спасибо»/«благодарю»/«очень помог») with NO decline/closure word («нет»/«всё»/«не надо») — so «всё, спасибо» / «нет, спасибо» stay closures (the operator follows up) while pure thanks acks. Dispatched before the funnel, gated on `not is_sales_intent` (so «спасибо, запишите» still books): `_handle_gratitude` → «Пожалуйста! Обращайтесь, если будут вопросы.» (localized), no escalation, state intact.

## Acceptance Criteria
1. Pure gratitude → courteous ack, not a handoff line. ✅
2. «всё/нет, спасибо» (closure) still hands off to the operator. ✅
3. «спасибо, запишите …» (gratitude + booking) still books. ✅
4. Gates green; 100% coverage. ✅

## Files
- `services/api/app/sales/sales_persona_answerer.py` (`is_gratitude` + `GRATITUDE_ACK_LINE` + dispatch branch + `_handle_gratitude`)
- tests: `test_sales_persona_answerer_early_busy_check.py`; updated `closing_restart` (non-gratitude reply for the stickiness test)
