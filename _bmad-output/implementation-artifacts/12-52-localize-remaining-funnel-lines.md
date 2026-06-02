# Story 12.52: Localize the remaining funnel handoff lines (round-11 N3, P3)

Status: review

## Story

As an **English-speaking customer who reaches a closing/cancellation/pitching-followup handoff**, I want **that line in English too**, so that **an English thread doesn't flip to Russian mid-conversation.**

**Problem (live, round 11):** an English booking thread got Russian replies — turn 1 «Спасибо! Передам детали коллегам…» (= `SCOPING_COMPLETE_HANDOFF_LINE`), turn 2 «Передам коллегам для подтверждения, на связи» (= `CLOSING_HANDOFF_LINE`).

## Root cause (CONFIRMED)

12.47 localized the scoping/busy/free/ask lines but **missed** several customer-facing emit sites. Grep of the deployed source: `SCOPING_COMPLETE_HANDOFF_LINE` bare at `_handoff_after_pitching_followup`, `CLOSING_HANDOFF_LINE` bare ×2, plus `CANCELLATION_HANDOFF_LINE` and `EMPTY_CATALOG_ESCALATION_LINE`. Reproduced: an English message in `STAGE_PITCHING` that isn't a counter-offer → `pitching_followup` → bare Russian line, with `ctx.language` correctly `"en"`. Because the round-11 QA was one continuous thread, the customer was mid-funnel (pitching/closing) by the English conversation, and (with the time unparsed pre-12.50) fell straight onto these un-localized lines. The 12.47 tests only asserted the sites it changed, so the gap was invisible.

## Fix

Localize the remaining funnel-reachable customer constants via the existing `localize(ru, en, language=ctx.language)`: add English variants for `CLOSING_HANDOFF_LINE`, `CANCELLATION_HANDOFF_LINE`, `EMPTY_CATALOG_ESCALATION_LINE` (`SCOPING_COMPLETE_HANDOFF_LINE_EN` already exists), and wrap the five bare emit sites (`_handle_cancellation`, `_handoff_after_pitching_followup`, the empty-catalog escalation, both closing sites).

## Acceptance Criteria

1. A pitching-followup handoff and a closing handoff reply in English on an English turn. ✅
2. Russian conversations unchanged (byte-identical). ✅
3. Gates green; 100% coverage. ✅

## Note on reachability

Story 12.50 (English parsing) now lets an English booking reach the busy/free verdict (already localized) instead of falling to these handoffs — so this is largely defence-in-depth plus the genuine closure path. `is_cancellation`/`is_out_of_scope` remain Russian-only detectors, so an English customer rarely reaches the cancellation line today; localizing it is correct-when-reached. Rare non-funnel fallbacks (`PROPOSAL_FALLBACK_*`, `EQUIPMENT_ACK_LINE`, `MATERIAL_DISPATCH_FALLBACK_LINE`, `PRICING_MISS_FALLBACK`) remain Russian — out of the booking journey, deferred.

## Tasks / Subtasks

- [x] EN variants for closing / cancellation / empty-catalog; wrap the five emit sites in `localize(..)`.
- [x] Tests (TDD): pitching-followup in EN → `SCOPING_COMPLETE_HANDOFF_LINE_EN`; closing in EN → `CLOSING_HANDOFF_LINE_EN`.

## Dev Notes

- All five sites already had `ctx` in scope; no signature changes.
- **Files:** `services/api/app/sales/sales_persona_answerer.py`.

## References

- Round-11 live QA Defect N3 (carry-over). Completes Story 12.47.
- [Source: sales_persona_answerer.py#_handoff_after_pitching_followup], [#_handle_closing], [#_handle_cancellation].

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–3).** The pitching-followup, closing, cancellation and empty-catalog handoffs now mirror the turn's language; Russian stays byte-identical. TDD; full suite green at 100% coverage.

### File List

- `services/api/app/sales/sales_persona_answerer.py` (modified — EN variants + localized emit sites)
- `tests/test_sales_persona_answerer_early_busy_check.py` (modified — EN pitching-followup + closing tests)
- `_bmad-output/implementation-artifacts/12-52-localize-remaining-funnel-lines.md` (new)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified)
