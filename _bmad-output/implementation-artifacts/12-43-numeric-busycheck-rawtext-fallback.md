# Story 12.43: Busy check falls back to the raw message text (round-8 N1, P1)

Status: review

## Story

As a **customer who names a date as `03.06`**, I want the bot to **check that slot against the calendar exactly like `3 июня` / `завтра`**, so that **a busy numeric date is rejected, not silently accepted.**

**Problem (live, round 8, 2 Jun 2026):**
```
Можно 03.06 в 16:00 на багги, нас двое?      → Спасибо! Передам детали коллегам…   (accepted, no check)  ❌
А завтра в 16:00 свободно для багги?          → …уже занято… 3 июня, 08:00.         (rejected)            ✅
Запишите на багги 03.06 в 14:00 … одна багги. → …уже занято… 3 июня, 08:00.         (rejected)            ✅
```
16:00 on 3 June is inside the 13:00–16:30 booking, yet the numeric `03.06 в 16:00` question was handed off unchecked.

## Root cause (CONFIRMED — refutes the doc's hypothesis)

The numeric parser (Story 12.38) is **not** the problem: `extract_requested_start("03.06 в 16:00") → 2026-06-03 16:00` on every path. The bug is that all busy-check paths parse the **LLM-stored `intent.dates`**, which is **lossy** — the scoping LLM sometimes stores a numeric date *without* its co-located time (e.g. `dates="03.06"`). `extract_requested_start("03.06")` → `None` (date-only, by design), so `_check_requested_slot` / `_maybe_intercept_busy_slot` return `None` and the slot is handed off as bookable. A fully-scoped numeric date worked only because that turn happened to store date+time together. The **raw customer message always carries both**.

## Fix

`_dates_with_raw_fallback(intent, question, ctx)` — when the LLM's stored `dates` does **not** parse into a concrete start but the raw message does, adopt the raw message as `dates`. Applied at both extraction points (`_handle_greeting`, `_extract_and_merge`), so the enriched `intent.dates` flows into **every** downstream busy check unchanged (no threading). Conservative: only overrides when the stored value can't be parsed, so a clean LLM date (relative / written-month / already date+time) is left intact — relative/weekday verdicts (incl. the D9 fix) are unaffected.

## Acceptance Criteria

1. `extract_requested_start("03.06 в 16:00") → 2026-06-03 16:00` (already true — parser verified). ✅
2. «Можно 03.06 в 16:00 …?» (underscoped, no vehicle_count) → **занято**, identical to «завтра в 16:00». ✅
3. Numeric forms `03.06 / 3.6 / 03.06.2026 / 3/6 / 03/06` parse (parser already supports them); guarded against `HH.MM` clock ambiguity (12.38). ✅
4. A free numeric-date slot is confirmed, not declined. (Covered — the fallback only changes whether the check *runs*; the verdict is the engine's.)
5. **Verdict independent of phrasing and of which optional fields are present.** ✅ (the busy check no longer depends on the LLM faithfully copying the time into `intent.dates`.)
6. Gates green; 100% coverage.

## Why not enrich via `_merge_dates_from_customer_message`

That helper uses `date_parser.parse_russian_date_span`, which handles relative/written-month dates but **not numeric** `DD.MM` — so it under-matched the N1 case AND over-matched relative dates (it would overwrite a clean `завтра в 14:00`, breaking 4 greeting/scoping tests). `_dates_with_raw_fallback` uses `extract_requested_start` (handles all formats) with a parse-or-not guard, which is both broader (numeric) and safer (no override when the stored value already parses).

## Tasks / Subtasks

- [x] `_dates_with_raw_fallback` helper (guarded raw-text fallback via `extract_requested_start`).
- [x] Call it after extraction in `_handle_greeting` and `_extract_and_merge` (covers opener, scoping, awaiting-time).
- [x] Tests (TDD): opener + scoping numeric date where the LLM stored a time-less `dates` → busy verdict + calendar consulted; existing relative-date tests unaffected.

## Dev Notes

- **Single enrichment point per extraction** → all busy checks (`_maybe_intercept_busy_slot`, `_check_requested_slot`, `_should_ask_for_time`) benefit without signature changes.
- **tz for the guard:** uses `ctx.timezone` only to decide *parses-or-not*; the downstream check re-parses with the real project tz, so the result is unchanged.
- **Files:** `services/api/app/sales/sales_persona_answerer.py`.

## References

- Round-8 live QA Defect N1.
- [Source: sales_persona_answerer.py#_dates_with_raw_fallback], [#_handle_greeting], [#_extract_and_merge].
- Parser: Story 12.38 (`extract_requested_start` numeric support).

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–6).** The deterministic busy check now falls back to the raw message text when the LLM's `intent.dates` is unparseable, so a numeric date is checked regardless of phrasing or which optional fields are present.
- **Root cause was the lossy LLM `intent.dates`, not the parser** (the doc's "numeric only on completion path" hypothesis was refuted by probing the parser).
- **Discarded** the `_merge_dates_from_customer_message` approach (wrong parser: under-matched numeric, over-matched relative — broke 4 tests). TDD; full suite green at 100% coverage.

### File List

- `services/api/app/sales/sales_persona_answerer.py` (modified — `_dates_with_raw_fallback` + 2 call sites)
- `tests/test_sales_persona_answerer_early_busy_check.py` (modified — 2 numeric-fallback tests)
- `_bmad-output/implementation-artifacts/12-43-numeric-busycheck-rawtext-fallback.md` (new)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified)
