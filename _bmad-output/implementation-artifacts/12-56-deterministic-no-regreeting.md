# Story 12.56: Deterministically suppress a mid-thread re-greeting (round-13)

Status: review

## Story

As a **customer mid-conversation who switches intent without greeting** («А можно перенести бронь на другой день?»), I want **the bot to answer directly**, so that **it doesn't open with «Здравствуйте» again.**

**Problem (live, round 13):** scenario 4 (reschedule) still opened with «Здравствуйте.» despite the round-12 fix (12.55).

## Root cause (CONFIRMED)

Reproduced against the deployed code: «перенести бронь» is a sales intent (`is_sales_intent=True`, not cancellation), so from `STAGE_CLOSING` it re-enters `_handle_greeting(returning=True)` — the directive from 12.55 *was* in the prompt. But 12.55's directive is **soft**: the LLM (gemini-2.5-flash-lite) greeted anyway. A prompt request can't guarantee the salutation is dropped.

(Also confirmed this round: the round-12 mixed-intent fix **12.54 works** — the suffix appends deterministically in both fresh and mid-thread repros; the round-13 «Это время свободно…» note truncated the appended decline line. And N2 stays deferred — data-blocked, per the standing decision.)

## Fix

Back the soft directive with a **deterministic strip**: when re-entering greeting mid-thread, run `_strip_leading_greeting` over the LLM reply to remove a leading Russian salutation (Здравствуйте/Привет/Добрый день/…) and its trailing punctuation, re-capitalising the remainder. A greeting-only reply is kept (never emptied).

Crucially, suppression is gated on **the customer not greeting first**: `suppress_greeting = returning and _LEADING_GREETING_RE.match(question) is None`. If the customer opens with «Здравствуйте!», greeting back is natural (and the round-9 R9-1 "price→greeting" reply «Здравствуйте! Какие даты?» is preserved). Both the directive and the strip use this gate.

## Acceptance Criteria

1. A mid-thread re-entry where the customer did NOT greet → the reply has no leading salutation (deterministic, not LLM-dependent). ✅
2. A mid-thread re-entry where the customer DID greet → the bot greets back (R9-1 preserved). ✅
3. First contact / new conversation still greets. ✅
4. `_strip_leading_greeting` is a no-op on a non-greeting reply and keeps a greeting-only reply intact. ✅
5. Gates green; 100% coverage. ✅

## Bundled regression guard (K1)

Round-13 also surfaced a positive: the nearest-free alternative is **current-time aware** (busy at 09:41 → «10:00», not the day's opening hour). Added `test_nearest_free_alternative_is_now_aware` (pins `now`, asserts the proposed slot ≥ now and never the pre-now opening) so it can't silently revert.

## Tasks / Subtasks

- [x] `_LEADING_GREETING_RE` + `_strip_leading_greeting`; gate `suppress_greeting` on returning AND customer-didn't-greet; apply to the directive and the reply text in `_handle_greeting`.
- [x] Tests (TDD): strip unit matrix (salutation / non-greeting / greeting-only); returning re-entry strips; first contact keeps; (R9-1 preserved — customer greeted → greet back). K1 now-aware guard.

## Dev Notes

- 12.55's directive is retained (it reduces how often the LLM greets, so the strip rarely has work to do) but is no longer the guarantee — the strip is.
- The gate on the customer's own greeting is what fixes the R9-1 regression my first (blanket) attempt introduced.
- **Files:** `services/api/app/sales/sales_persona_answerer.py`.

## References

- Round-13 live QA, scenario 4 (re-greet persists) + new positive K1. Completes Story 12.55.
- [Source: sales_persona_answerer.py#_handle_greeting], [#_strip_leading_greeting].

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–5).** A mid-thread re-entry no longer re-greets (deterministic strip + soft directive), while a customer who greets is greeted back and first contact is unchanged. Root-caused as LLM non-adherence to the soft directive. Bundled the K1 now-aware-alternative guard. TDD; full gate green at 100% coverage.

### File List

- `services/api/app/sales/sales_persona_answerer.py` (modified — `_strip_leading_greeting` + `suppress_greeting` gate)
- `tests/test_sales_persona_answerer_early_busy_check.py` (modified — strip/return/first-contact tests + K1 guard)
- `_bmad-output/implementation-artifacts/12-56-deterministic-no-regreeting.md` (new)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified)
