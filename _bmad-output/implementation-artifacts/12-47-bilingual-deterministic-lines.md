# Story 12.47: Deterministic customer lines mirror the turn's language (round-10 N3, P3)

Status: review

## Story

As an **English-speaking customer**, I want **every reply in English, on every turn**, so that **an English thread doesn't revert to Russian the moment the bot emits a fixed line.**

**Problem (live, round 10, 2 Jun 2026, 20:49–20:50):** the first English message got an English reply (the LLM line mirrored it), but the next English turn got «Уточните, пожалуйста, желаемые дату и время…» — Russian. Language was mirrored for LLM-generated lines but not for the **deterministic** customer-facing constants.

## Root cause (CONFIRMED)

Story 12.45 made the **LLM** lines (greeting / scoping / pricing) mirror the customer's language by appending `reply_language_directive(question)` to the system prompt — per turn, correct. But the persona also emits many **deterministic** constants (ask-for-time, busy/off-hours/free/unverified verdicts, handoff confirmations, the nearest-free alternative tail, the accept confirmation, the out-of-scope decline). These were **Russian-only string literals**. The moment any of them fired on an English turn, the reply was Russian — exactly the round-10 ask-for-time case.

## Fix

Pin the turn's language **once** and select the variant of each deterministic line from it.

1. **Per-turn detection.** At the top of `try_answer`, `ctx = replace(ctx, language=detect_language(question))` — the same cheap heuristic the LLM directive uses (Latin-dominant → `en`, else `ru`). `AnswerContext.language` is threaded everywhere, so every downstream helper sees it.
2. **`localize(ru, en, *, language)`** (in `reply_language.py`) returns the English variant for `language == "en"`, else the Russian default — so Russian conversations are byte-identical.
3. **English variants** for the booking-journey customer constants: `ASK_FOR_TIME_LINE`, `SLOT_BUSY_LINE` + the four unavailable-reason lead lines, `SLOT_FREE_HANDOFF_LINE`, `SCOPING_COMPLETE_HANDOFF_LINE`, `SLOT_UNVERIFIED_HANDOFF_LINE`, `PITCHING_ACCEPT_CONFIRM_LINE`, `OUT_OF_SCOPE_DECLINE_LINE`, plus an English `_MONTHS_EN` map so the nearest-free tail and the accept confirmation name the slot in English word-order ("May 30, 09:00").

Every emit site now passes `ctx.language` through `localize(...)`. The LLM lines already mirrored per turn (12.45) and are unchanged.

## Acceptance Criteria

1. Reply language is computed per turn, so an English thread stays English on EVERY turn (the ask-for-time, the busy/free verdict, the handoff), not just the first. ✅
2. EN booking with a concrete parseable time → an availability verdict in English (busy lead + English nearest-free tail). ✅
3. Russian conversations unchanged — every Russian line is byte-identical. ✅ (`localize` returns the RU default for `ru` / unknown languages)
4. Gates green; 100% coverage. ✅

## Out of scope (follow-up)

- **Don't re-ask for the date when it was already supplied** (round-10 N3 secondary nit / AC2). The ask-for-time line deliberately asks for date+time TOGETHER because `intent_merge` REPLACES `dates` (a bare follow-up time would drop a previously-collected date — see the constant's note). Splitting the ask without losing the date is a separate change; tracked as a future story.
- Rare internal fallbacks not on the booking happy-path (e.g. `PROPOSAL_FALLBACK_*`, `EMPTY_CATALOG_ESCALATION_LINE`, `CANCELLATION_HANDOFF_LINE`, `CLOSING_HANDOFF_LINE`) remain Russian for now; they fall outside the English booking journey this story covers.

## Tasks / Subtasks

- [x] `localize(ru, en, *, language)` helper + per-turn `replace(ctx, language=detect_language(question))` at the `try_answer` boundary.
- [x] English variants + `_MONTHS_EN`; wire `_ask_for_time`, `_propose_alternative_or_handoff`, `_handoff_after_scoping`, `_handoff_unverified_slot`, `_confirm_slot`, `_handle_out_of_scope`.
- [x] Tests (TDD): `localize` (en / ru / unknown); EN busy verdict + English nearest-free tail; EN free handoff; EN ask-for-time; EN accept confirmation names the slot in English; RU verdict unchanged (regression).

## Dev Notes

- Detection happens **once** per turn (not per line) — a single source of truth for the whole reply, LLM and deterministic alike.
- Word-order differs by language ("31 мая" vs "May 31"), so the alternative tail and the accept confirmation branch on `ctx.language` rather than swapping only the month token.
- An English customer often won't reach a verdict at all because `extract_requested_start` is Russian-centric — they hit the (now English) ask-for-time. The verdict variants cover the case where the date parses (numeric, or an LLM-canonicalised date).
- **Files:** `services/api/app/sales/reply_language.py`, `services/api/app/sales/sales_persona_answerer.py`.

## References

- Round-10 live QA Defect N3 (and round-9 note: the EN→«Уточню у коллег» was a deterministic constant). Builds on Story 12.45 (LLM-line mirroring).
- [Source: reply_language.py#localize], [sales_persona_answerer.py#try_answer], [#_ask_for_time], [#_propose_alternative_or_handoff].

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–4).** The turn's language is pinned once and every deterministic booking-journey line is localized through it, so an English thread stays English on every turn while Russian stays byte-identical. AC2 (don't re-ask the date) and the rare non-journey fallbacks are scoped out as follow-ups. TDD; full suite green at 100% coverage.

### File List

- `services/api/app/sales/reply_language.py` (modified — `localize` helper)
- `services/api/app/sales/sales_persona_answerer.py` (modified — per-turn language, EN constants, `_MONTHS_EN`, localized emit sites)
- `tests/test_sales_reply_language.py` (modified — `localize` unit tests)
- `tests/test_sales_persona_answerer_awaiting_time.py` (modified — EN verdict/free/ask/accept + RU regression tests)
- `_bmad-output/implementation-artifacts/12-47-bilingual-deterministic-lines.md` (new)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified)
