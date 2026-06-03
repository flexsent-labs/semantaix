# Story 12.55: No re-greeting on a mid-thread intent switch (round-12, cosmetic)

Status: review

## Story

As a **customer already in conversation**, I want **the bot to keep talking, not say «Здравствуйте» again**, so that **an intent switch (a fresh booking after a handoff, re-engaging after dormancy) reads naturally.**

**Problem (live, round 12):** some intent switches mid-thread open with «Здравствуйте.» — cosmetic but jarring.

## Root cause (CONFIRMED)

`_handle_greeting` is re-entered mid-thread from three `_dispatch` branches — `STAGE_DORMANT` re-engage, the pricing "moved-on" re-route (12.46), and a fresh sales intent from `STAGE_CLOSING` (12.23) — and its LLM prompt (`_build_greeting_prompt`) is written for first contact ("Это первый ответ клиенту…"), so the model greets. The same handler serves first contact and re-entry with no signal to distinguish them.

## Fix

Add a `returning: bool = False` parameter to `_handle_greeting`. When `True`, append `_RETURNING_NO_GREETING_DIRECTIVE` to the system prompt ("клиент уже в этом диалоге — НЕ здоровайтесь повторно…"), the same append mechanism as `reply_language_directive`. The three mid-thread call sites pass `returning=True`; first contact (`state is None`) and `STAGE_NEW` keep the default `False`.

## Acceptance Criteria

1. A mid-thread greeting re-entry (dormant / pricing-moved-on / closing-fresh-intent) carries the no-greeting directive in the prompt. ✅
2. First contact / new conversation does NOT carry it (still greets). ✅
3. On-topic extraction/answering is unchanged (directive only suppresses the salutation). ✅
4. Gates green; 100% coverage. ✅

## Dev Notes

- The greeting line is LLM-generated, so the fix is a prompt directive (the testable seam is the system prompt the LLM receives, asserted via the recorded `complete_json` call — the same pattern as the language-directive tests). Whether the model obeys is a prompt-quality matter, not a deterministic assertion.
- Directive is written in Russian (matches the base prompt); it suppresses the salutation regardless of the reply language.
- **Files:** `services/api/app/sales/sales_persona_answerer.py`.

## References

- Round-12 live QA, "Minor: occasional mid-conversation re-greeting". Related: 12.23 (closing→fresh intent), 12.46 (pricing not sticky).
- [Source: sales_persona_answerer.py#_handle_greeting], [#_dispatch].

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–4).** Mid-thread greeting re-entries now instruct the LLM not to re-greet; first contact still greets. TDD on the prompt seam; full suite green at 100% coverage.

### File List

- `services/api/app/sales/sales_persona_answerer.py` (modified — `returning` param + `_RETURNING_NO_GREETING_DIRECTIVE` + 3 call sites)
- `tests/test_sales_persona_answerer_early_busy_check.py` (modified — returning/first-contact prompt tests)
- `_bmad-output/implementation-artifacts/12-55-no-regreeting-midthread.md` (new)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified)
