# Story 12.45: Reply in the customer's language (round-8 N3 / D8, P3)

Status: review

## Story

As an **English-speaking customer**, I want **an English reply**, so that **I can understand it.**

**Problem (live, round 8):**
```
Hello! Can I book a buggy for 4 people on June 4 at 11am? How much?
→ Здравствуйте. Уточните, пожалуйста, какой сложности маршрут вас интересует?   (RU; fields/price dropped)
```

## Root cause

`AnswerContext.language` is a **configured default** (`_effective_default_language()`), not detected per message, and the sales persona's customer-facing text is **Russian-only**. The persona's conversational lines (greeting / scoping / pricing) are LLM-generated, but the prompts never told the model to mirror the customer's language, so it always answered in Russian.

## Fix (this story — the conversational core)

- `reply_language.detect_language(text)` — a cheap script heuristic: Latin-dominant → `"en"`, else `"ru"` (the default; empty / Cyrillic-dominant / mixed-but-mostly-RU → `"ru"`).
- `reply_language.reply_language_directive(text)` — a system-prompt suffix telling the LLM to reply in the customer's language; **empty for Russian**, so Russian prompts are byte-identical (no regression).
- Appended to the persona's three LLM prompts — **greeting**, **scoping** (`_extract_and_merge`), and **pricing** (`_render_price_hit`) — so an English customer gets English greeting/scoping/pricing replies.

## Acceptance Criteria

1. Detect the customer-message language and reply in it (RU/EN) for the persona's **LLM-generated** lines. ✅
2. Field extraction from English keeps working (unchanged — `is_sales_intent` already matches EN bookings, fields still parse). ✅
3. Russian conversations unchanged — the directive is empty for RU, so prompts are byte-identical. ✅
4. Gates green; 100% coverage. ✅

## Out of scope (documented follow-up → 12.46)

The persona's **deterministic constant** lines remain Russian: `SLOT_BUSY_LINE`, `SLOT_FREE_HANDOFF_LINE`, `SCOPING_COMPLETE_HANDOFF_LINE`, `OUT_OF_SCOPE_DECLINE_LINE`, the ask-for-time / cancellation lines, etc. An English customer who reaches a *busy verdict* or a *handoff* still sees those in Russian. Making them bilingual is a separable, mechanical effort (EN variants keyed on the detected language) tracked as a follow-up — this story delivers the conversational majority (greeting/scoping/pricing are LLM-generated and now mirror the language), which is what the live N3 case hit.

## Tasks / Subtasks

- [x] `reply_language.py`: `detect_language` + `reply_language_directive` (RU default → empty).
- [x] Append the directive to the greeting, scoping, and pricing LLM prompts (thread `question` into `_render_price_hit`).
- [x] Tests (TDD): detection matrix (EN / RU / mixed / empty); persona greeting EN → prompt carries the English directive; RU opener → prompt unchanged.

## Dev Notes

- **Non-regressive by construction:** the directive is `""` for Russian, so every existing Russian prompt/test is unchanged.
- **Detection is conservative:** mixed text that is mostly Russian stays Russian (only Latin-dominant flips to English), so a stray English word in a Russian message doesn't switch the language.
- **Files:** `services/api/app/sales/reply_language.py` (new), `services/api/app/sales/sales_persona_answerer.py`.

## References

- Round-8 live QA Defect N3 (carried from D8).
- [Source: sales_persona_answerer.py#_handle_greeting], [#_extract_and_merge], [#_render_price_hit]; [reply_language.py].

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–4).** The persona's LLM-generated lines now mirror the customer's language (EN/RU); Russian is unchanged (empty directive). The live N3 case (EN booking → RU scoping question) is fixed.
- **Scoped honestly:** deterministic constant lines (busy/handoff/decline) remain RU and are tracked as a bilingual-constants follow-up (12.46) — documented above.
- **TDD; 100% coverage** on the new module; full suite green.

### File List

- `services/api/app/sales/reply_language.py` (new)
- `services/api/app/sales/sales_persona_answerer.py` (modified — directive on greeting/scoping/pricing prompts)
- `tests/test_sales_reply_language.py` (new), `tests/test_sales_persona_answerer_greeting.py` (modified — EN/RU directive tests)
- `_bmad-output/implementation-artifacts/12-45-reply-in-customer-language.md` (new)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified)
