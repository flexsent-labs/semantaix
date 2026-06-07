# Story 12.95: A talk-to-a-human request gets human-handoff copy, not booking copy (round-27 R27-3)

Status: review

## Story
As a customer who says «Можно поговорить с живым человеком, а не с ботом?», I want a human-escalation acknowledgement («Конечно, передаю ваш запрос менеджеру — скоро свяжутся»), not the booking-flavored «Передам детали коллегам на подтверждение…» (which implies a booking).

## Root cause (CONFIRMED)
Reproduced against the deployed dispatch: there is no "talk to a human" detector. The message fell through to the funnel/pitching handoff, which used the booking-completion copy. It DID reach a person (escalation), but the wording presumed a booking. Verified live: the message is `is_sales_intent` = True, so a `not is_sales_intent` gate would never fire — the new branch must be ungated and precise.

## Fix
- `is_human_request` (precise regex `_HUMAN_REQUEST_RE`): a human/operator/manager/специалист/сотрудник noun, or «живой/реальный/настоящий человек», or «не (с) бот/робот». It deliberately does NOT match bare «человек», so a field answer like «нас двое человек» is never swallowed.
- `_handle_human_request` → `HUMAN_REQUEST_LINE` («Конечно, передаю ваш запрос менеджеру - скоро свяжутся с вами.»), escalate=True, hitl_reason=`sales_human_request`, carries the verbatim question as `escalation_context`, `suppress_followup`. Funnel state is left intact (no stage change) so a customer who resumes booking isn't re-greeted.
- Dispatch branch placed right after cancellation, BEFORE the funnel, ungated (the detector is precise; a human handoff is valid even with a booking in flight).

## Acceptance Criteria
1. «поговорить с живым человеком / позовите менеджера / с оператором» → `HUMAN_REQUEST_LINE`, escalate=True, hitl_reason=sales_human_request; never the booking-completion copy («на подтверждение»). ✅
2. «нас двое человек» / «хочу багги на 8 человек» are NOT matched. ✅
3. Fires in any stage (incl. pitching); funnel state untouched; English localized line. ✅
4. Gates green; 100% coverage. ✅

## Files
- `services/api/app/sales/sales_persona_answerer.py` (`HITL_REASON_HUMAN_REQUEST`, `HUMAN_REQUEST_LINE`/`_EN`, `HUMAN_REQUEST_ESCALATION_CONTEXT`, `_HUMAN_REQUEST_RE`, `is_human_request`, `_handle_human_request`, dispatch branch, `__all__`)
- tests: `test_sales_persona_answerer_early_busy_check.py`
