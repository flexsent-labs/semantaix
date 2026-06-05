# Story 12.74: Gibberish gets a clarification (in Russian), not an English handoff (round-18 R18-6)

Status: review

## Story
As a customer who sends unintelligible input («asdfgh qwerty фыва 123 ???»), I want a clarification in my language — not a booking-completion handoff, and not a reply that flips to English because of stray Latin tokens.

## Root cause (CONFIRMED)
1. **Language misfire.** `detect_language` returned `"en"` whenever `latin > cyrillic`; the gibberish has 12 Latin vs 4 Cyrillic → English, despite the Cyrillic «фыва».
2. **Misroute.** A mid-funnel (pitching) state routes any non-counter/non-closure reply to `_handoff_after_pitching_followup`; gibberish wasn't distinguished from a genuine "I'll think about it" → a booking handoff.

## Fix
1. **Language** — `detect_language` now stays Russian whenever ANY Cyrillic is present; only a purely-Latin message → `"en"`. A Cyrillic-context conversation can't be flipped by stray Latin/gibberish tokens.
2. **Gibberish guard** — `is_gibberish` (using `RussianNormalizer.is_known_word`, pymorphy-backed) is True only when the message has Cyrillic context AND not a single word-like token is a known Russian word. Dispatched before the funnel, gated on `not is_sales_intent`, so a real (even typo-heavy «заброниравать баги завтре») booking — which has a known word — is never swallowed. `_handle_gibberish` → «Извините, не совсем понял. Подскажите, пожалуйста, дату, время и услугу?» (localized), no escalation.

## Acceptance Criteria
1. «asdfgh qwerty фыва 123 ???» → a Russian clarification, never a handoff and never English. ✅
2. Same in a pitching state (the live scenario) — clarify, not the pitching-followup handoff. ✅
3. A real/typo-heavy booking or a bare service word («багги») is NOT treated as gibberish. ✅
4. Existing language cases unchanged (pure-English → en; mixed/Cyrillic → ru). ✅
5. Gates green; 100% coverage. ✅

## Files
- `services/api/app/sales/reply_language.py` (`detect_language`)
- `services/api/app/russian_text/normalizer.py` (`is_known_word`)
- `services/api/app/sales/sales_persona_answerer.py` (`is_gibberish`, `GIBBERISH_CLARIFY_LINE`, dispatch branch, `_handle_gibberish`, `_Normalizer` protocol)
- tests: `test_sales_reply_language.py`, `test_russian_normalizer.py`, `test_sales_persona_answerer_early_busy_check.py`
