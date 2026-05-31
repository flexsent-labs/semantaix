# Story 12.21: Counter-offer beats acceptance in PROPOSING (don't mis-accept "давайте на <date>")

Status: review

## Story

As a **customer who was just offered a booking date**,
I want my reply that names a **different** date ("давайте на 1 июня") to be understood as a **counter-offer for that date**,
so that **the bot re-proposes for the date I asked for instead of confirming the slot it already offered**.

**Problem (same mis-accept class fixed for PITCHING in Story 12.19, now in PROPOSING):**

```
Bot:      Предлагаю на 1 мая с началом в 14:00.
Customer: давайте на 1 июня                  ← counter-offer for a DIFFERENT date
Bot:      Передам коллегам для подтверждения, на связи.   ← BUG (closed on 1 мая, ignored 1 июня)
```

**Root cause** (`services/api/app/sales/sales_persona_answerer.py`, `_handle_proposing` `:2017`): acceptance is checked **before** merging a counter-offer date. Because the acceptance seed list (`data/russian_sales_acceptance.txt`) includes `давайте`, a reply like "давайте на 1 июня" — an acceptance lemma **plus** a different parseable date (`parse_russian_date_span("давайте на 1 июня")` → `2026-06-01`) — satisfies `is_acceptance(...)` first and transitions to `closing` (confirming the already-proposed slot) instead of re-proposing for 1 June.

This is the inverse of the ordering Story 12.19 (PR #99) deliberately established for `_handle_pitching` (`:924`), where the counter-offer check runs **before** acceptance precisely so "давайте на 1 июня" is not mis-accepted. PROPOSING was missed; this story brings it in line.

## Acceptance Criteria

1. **Counter-offer beats acceptance.** Given PROPOSING with a prior proposal (`last_proposal is not None`), when the reply is BOTH an acceptance lemma AND carries a new/different parseable date ("давайте на 1 июня"), the counter-offer wins: `dates` is updated via the existing `_merge_dates_from_customer_message`, the proposer re-runs with the new date, and a fresh slot is rendered. The stage stays `proposing`. It must NOT transition to `closing`.
2. **Pure acceptance still closes.** A reply that is an acceptance lemma with NO new/different parseable date ("да", "да, согласен", "давайте") still transitions to `STAGE_CLOSING` via `_transition_to_closing` (escalate, `hitl_reason=sales_closing_handoff`, `CLOSING_HANDOFF_LINE`) — unchanged from Story 12.07.
3. **Non-acceptance counter-offer unchanged.** A plain counter-offer with no acceptance lemma ("лучше 2 мая") still re-proposes with the updated date — regression-safe.
4. **First-turn guard unchanged.** A bare "да" with no prior proposal (`last_proposal is None`) still falls through to a normal proposing dispatch — never closing (Story 12.07 guard preserved).
5. **Ordering mirrors `_handle_pitching` (12.19).** `merged_intent` is computed first; the acceptance branch is only taken when `merged_intent.dates == existing_intent.dates` (i.e. the reply carries no new/different parseable date). `merged.dates != existing.dates` is the same counter-offer predicate used by `_handle_pitching`.
6. **Gates green.** `ruff` clean; full suite at 100% coverage on `services/`; a new TDD test covers the "давайте на <different date>" → re-proposes case.

## Tasks / Subtasks

- [ ] **Reorder `_handle_proposing`** (AC 1,2,4,5) — move the `now`/`_merge_dates_from_customer_message` call above the acceptance check; guard the acceptance branch with `merged_intent.dates == existing_intent.dates and last_proposal is not None and is_acceptance(...)`. A new/different parseable date now falls through to `propose(...)` with `merged_intent`, exactly as a non-acceptance counter-offer already does.
- [ ] **TDD test** (AC 1,6) — in `tests/test_sales_persona_answerer_acceptance.py`, add a test seeded with a prior proposal where the reply "давайте на 1 июня" re-proposes (proposer saw `1 июня`; `stage_after == proposing`; `hitl_reason != sales_closing_handoff`). The existing acceptance / counter-offer / first-turn tests stand as regression guards for AC 2–4.

## Dev Notes

- **Files:** `services/api/app/sales/sales_persona_answerer.py` (`_handle_proposing` `:2017` only). No new constants, no data-file changes, no schema changes.
- **Reuse, don't reinvent:** `_merge_dates_from_customer_message` (`:2063`, wraps `parse_russian_date_span`); `is_acceptance` (`acceptance.py` — `давайте` is already a seed in `data/russian_sales_acceptance.txt`); `_transition_to_closing`. Mirrors the `_handle_pitching` counter-offer-before-acceptance ordering (`:924`, Story 12.19).
- **Why the guard is exact:** `_merge_dates_from_customer_message` returns the unchanged `existing_intent` when the reply has no parseable date, otherwise `replace(existing_intent, dates=question.strip())`. So `merged.dates != existing.dates` is true iff the reply carries a new/different parseable date — the precise "counter-offer" condition, identical to `_handle_pitching`.
- **Conventions:** answerer dispatch never raises; immutable `Intent` (`replace`); time injected via `self._clock()`; `from __future__ import annotations`; ruff E/F/I line-100; 100% coverage gate.

### Project Structure Notes

- Change is localized to a single method and its tests. The counter-offer path reuses the existing `propose` → `_render_and_persist_proposal` / `_handle_no_proposal` flow with no new sinks.

### References

- [Source: services/api/app/sales/sales_persona_answerer.py#_handle_proposing] · [Source: services/api/app/sales/sales_persona_answerer.py#_merge_dates_from_customer_message]
- [Source: services/api/app/sales/acceptance.py#is_acceptance] · [Source: data/russian_sales_acceptance.txt]
- Precedent / pattern mirrored: `_bmad-output/implementation-artifacts/12-19-recognize-slot-confirmation-and-stop-pitching-reask-loop.md` (AC 3 "Counter-offer beats acceptance"; `_handle_pitching` ordering).

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–6).** `_handle_proposing` now computes `merged_intent` via `_merge_dates_from_customer_message` **before** the acceptance check, and the acceptance branch is guarded with `merged_intent.dates == existing_intent.dates`. A reply carrying a new/different parseable date ("давайте на 1 июня") no longer matches the acceptance branch (despite the `давайте` lemma) and falls through to `propose(...)` with the counter-offer date — re-proposing for 1 June instead of closing on the already-offered 1 May slot. Mirrors the `_handle_pitching` ordering from Story 12.19.
- **Regression-safe (AC 2–4):** pure acceptance ("да", "да, согласен", "давайте") still closes — `merged.dates == existing.dates` holds when no date is parsed; a first-turn bare "да" with no `last_proposal` still dispatches normally; a non-acceptance counter-offer ("лучше 2 мая") is unchanged.
- **TDD:** `test_acceptance_lemma_with_new_date_re_proposes_not_closing` was written first and watched fail (`AssertionError: 'closing' == 'proposing'` — proposer never ran), then the reorder made it green. The existing acceptance / counter-offer / first-turn-guard tests stand as regression guards.
- `ruff` clean; full suite **3031 passed at 100% coverage** (CI parity: `pytest --cov --cov-config=.coveragerc`).

### File List

- `services/api/app/sales/sales_persona_answerer.py` (modified — reordered `_handle_proposing`: merge dates before the acceptance check; guard acceptance with `merged_intent.dates == existing_intent.dates`)
- `tests/test_sales_persona_answerer_acceptance.py` (modified — new `test_acceptance_lemma_with_new_date_re_proposes_not_closing`)
- `_bmad-output/implementation-artifacts/12-21-proposing-counter-offer-before-acceptance.md` (new — this story)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified — add `12-21-proposing-counter-offer-before-acceptance: review`)
