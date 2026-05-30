# Story 12.11: Graceful decline + per-field loop guard in scoping

Status: ready-for-dev

## Story

As a **customer being scoped for a booking**,
I want to be able to say a field **doesn't apply** ("не нужно", "0", "нет") and have the bot move on,
so that **I'm never stuck re-answering the same question forever**.

**Problem (observed live):** the funnel asked "Сколько нужно водителей?"; the customer answered "0" then "не нужно", and the bot re-asked the same question every turn. Cause: all five scoping fields are mandatory (`Intent.is_complete()` needs every one non-`None`) and the scoping prompt says *"omit the key if not named, never invent"* — so a decline records nothing → `drivers` stays `None` → the funnel never completes → it re-asks indefinitely. There is no decline path and no loop guard.

## Acceptance Criteria

1. **Decline records a sentinel.** Given the bot just asked for the topmost-missing scoping field, when the customer's reply is a decline/negation for that field ("нет" / "не нужно" / "не требуется" / "не надо" / a bare "0"), then the answerer records a sentinel value (e.g. `"не требуется"`) for **that** field and advances to the next missing field (or completes).
2. **Per-field loop guard.** Given the same scoping field has been asked twice without being filled, when the bot would ask it a third time, then it instead auto-fills the sentinel for that field and advances — no field is ever asked more than twice.
3. **Sentinel surfaces to the operator.** The sentinel value flows into the booking summary the HITL ticket carries (`escalation_context`), so a human sees e.g. "drivers: не требуется".
4. **Normal answers unaffected.** A real value ("трое", "сами", "средняя") is extracted and stored as today; decline detection must not swallow a real answer (e.g. "нет, троих" must still capture the count, not decline).
5. **Gates green.** `ruff` clean; full suite 100% coverage; new tests for decline-on-current-field, the loop guard, and the "no false decline on a real answer" case.

## Tasks / Subtasks

- [ ] **Decline detector** (AC: 1, 4) — reuse `closure.is_no_more` or add a sibling `is_decline` reading a `data/russian_sales_decline.txt` lemma set ("нет / не нужно / не требуется / не надо / 0"). Keep it conservative — only fire when the reply is *purely* a decline (avoid swallowing "нет, троих"); unit-test the boundary.
- [ ] **Track the asked field for the loop guard** (AC: 2) — persist the topmost-missing field the bot last asked. Lightest option: add a `last_asked_field` column to the sales state row (state_repository + `_persist`), OR stash it in the existing `last_proposal` JSON. On a scoping turn, if `last_asked_field == current_topmost_missing` and it's still missing after extraction → loop → auto-fill sentinel.
- [ ] **Wire into `_handle_scoping`** (AC: 1, 2, 3) — after `_extract_and_merge`, if not complete: compute `current = merged.missing_fields()[0]`; if decline detected OR loop-guard tripped → fill `current` with the sentinel (`replace(merged, **{current: SENTINEL})`), re-check completeness, and either ask the next field or `_complete_booking`. Persist `last_asked_field` when asking.
- [ ] **Tests** — decline fills current field + advances; two-ask loop guard auto-fills the 3rd time; "нет, троих" keeps the count; sentinel appears in `escalation_context`; full funnel completes when the last field is declined. 100% coverage.

## Dev Notes

- **Files:** `services/api/app/sales/sales_persona_answerer.py` (`_handle_scoping` — the merge+complete branch, ~the `if not merged.is_complete()` block), `services/api/app/sales/closure.py` (+ new `data/russian_sales_decline.txt` or reuse closure set), `services/api/app/sales/state_repository.py` + the `_persist` call (if adding `last_asked_field`), `services/api/app/sales/intent.py` (a `with_field(name, value)` helper or use `dataclasses.replace`).
- **The topmost-missing field is deterministic** (`Intent.missing_fields()[0]`), so the bot always knows which field its question was for — no need to parse its own question text.
- **Sentinel choice:** a Russian string like `"не требуется"` (non-`None` → satisfies `is_complete`, reads sensibly in the operator summary). Document it as a constant.
- **Conventions:** answerers dispatch, never raise; immutable `Intent` (`replace`); time injected; `from __future__ import annotations`; ruff E/F/I line-100; 100% coverage gate.
- **Interaction with 12.12:** if a field is made optional there, it isn't asked at all; this story covers declines/loops for whatever stays required.

### References

- [Source: services/api/app/sales/sales_persona_answerer.py#_handle_scoping]
- [Source: services/api/app/sales/intent.py#is_complete / missing_fields]
- [Source: services/api/app/sales/closure.py#is_no_more + data/russian_sales_closure.txt]
- Live evidence: chat funnel stuck `scoping` with `drivers: null`, other 4 fields filled, re-asking "Сколько нужно водителей?".

## Dev Agent Record

### Agent Model Used

### Completion Notes List

### File List
