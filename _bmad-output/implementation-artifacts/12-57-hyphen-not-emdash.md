# Story 12.57: Customer copy uses a plain hyphen, not an em/en dash (round-14)

Status: review

## Story
As a customer, I want the bot's messages to use a plain «-», so the copy reads consistently (no «—»).

## Root cause
Customer-facing constants + the dynamic alternative tail used «—» (e.g. «свободно — передам», «время — 3 июня»).

## Fix
Changed the customer-facing string constants and `_format_alternative_tail` to use a plain hyphen «-» directly (source = output). NOT a runtime normalisation — verbatim content (RAG prices, concept descriptions, LLM `next_question`) is left untouched, so quoted text stays verbatim.

## Acceptance Criteria
1. Bot-authored lines use «-», never «—»/«–». ✅
2. Verbatim KB/LLM content is not altered. ✅ (no output post-processing)
3. Gates green; 100% coverage. ✅

## File List
- `services/api/app/sales/sales_persona_answerer.py` (constants + tail → «-»)
- `tests/test_sales_persona_answerer_early_busy_check.py` (hyphen guard test)
