# Story 12.20: Broaden Russian date/time parsing — ordinal dates and bare hours

Status: ready-for-dev

## Story

As a **customer restating a date/time in shorthand** ("на 31-ое в 8", "2-го июня в 9"),
I want the bot to **understand ordinal day-of-month dates and bare hours**,
so that **it can re-verify my requested time against the calendar instead of falling back to a generic handoff**.

**Problem:** the conservative extractors only accept relative-day words (`сегодня`/`завтра`/`послезавтра`) or weekday names plus an `HH:MM`/"N час" clock. They reject ordinal dates ("31-ое") and bare hours ("в 8"), so a restated *different* slot can't be auto-checked. Builds on Story 12.19 (which already handles the reported confirmation via acceptance). Depends on 12.19.

## Acceptance Criteria

1. **Bare-hour clock.** `extract_requested_start` parses "в 8" → 08:00; honors qualifiers — "в 8 утра"→08:00, "в 8 вечера"/"в 8 ночи"→20:00; bare = literal 24h hour; out-of-range (>23) → `None`. Existing `HH:MM`/"N час" unchanged.
2. **Ordinal day-of-month.** `extract_requested_start` resolves "31-ое"/"31-го"/"31-е"/"31 числа" to an absolute date (current month; roll to the next month that has that day if already past); invalid (e.g. 31 in a 30-day month) handled via `_safe_date`. Existing relative-word/weekday resolution unchanged.
3. **Span parser parity.** `parse_russian_date_span` recognizes the ordinal day-of-month shape → `(date, date)`, so `_merge_dates_from_customer_message` detects ordinal counter-offers. Existing shapes unchanged.
4. **PITCHING autonomous re-verify + name slot.** With 12.19's counter-offer branch, a restated concrete time now parses: a free slot is confirmed **naming it** (thread an optional `confirm_start` through `_complete_booking` → `_handoff_after_scoping`; when free and set, use `PITCHING_ACCEPT_CONFIRM_LINE`); a busy slot offers (and persists) a new alternative.
5. **No regressions in other callers.** `extract_requested_start` (also used by `CalendarAvailabilityAnswerer`, `services/api/app/calendar/availability_answerer.py:179`) and `parse_russian_date_span` (also used by `services/api/app/sales/date_proposer.py:152` and the scheduler's proactive-followup via `services/api/app/sales/russian_dates.py`) keep all existing tests green.
6. **Gates green.** `ruff` clean; full suite 100% coverage; new parser unit tests + a PITCHING re-verify test.

## Tasks / Subtasks

- [ ] **`extract_requested_start`** (AC 1,2) in `services/api/app/calendar/service_resolver.py` — add a bare-hour clock branch (with утра/вечера/ночи disambiguation) to `_extract_clock`; refactor day resolution to yield a target `date` from either an ordinal (absolute, with month-roll + `_safe_date`) or the existing relative/weekday offset.
- [ ] **`parse_russian_date_span`** (AC 3) in `services/api/app/sales/date_parser.py` — add an ordinal day-of-month regex → `(that_date, that_date)`.
- [ ] **PITCHING re-verify + naming** (AC 4) — in `_handle_pitching` (12.19), when the message yields a concrete `requested_start`, pass it as `confirm_start` to `_complete_booking`; thread `confirm_start` to `_handoff_after_scoping` so the free branch names the slot (scoping-complete path passes `None` → unchanged).
- [ ] **Tests** (AC 6) — add ordinal + bare-hour cases (incl. month-roll, invalid-day, утра/вечера, out-of-range) to `tests/test_calendar_service_resolver.py` and `tests/test_sales_date_parser.py`; add a PITCHING test where "давайте на 2-ое в 9" re-verifies a *different* free slot and names it. Confirm `tests/test_calendar_availability_answerer.py`, `tests/test_requested_time_check.py`, and date-proposer/proactive-followup tests stay green.

## Dev Notes

- **Files:** `services/api/app/calendar/service_resolver.py` (`extract_requested_start`), `services/api/app/sales/date_parser.py` (`parse_russian_date_span`), `services/api/app/sales/sales_persona_answerer.py` (`confirm_start` thread).
- **Ambiguity (в 8 = 08:00 or 20:00):** default bare hour to literal 24h (matches how slots are offered, e.g. 08:00); honor утра/вечера/ночи. Mitigated by 12.19 naming the slot back to the customer, the acceptance safety-net, and the operator finalizing (autonomous booking stays out of scope).
- **Blast radius is additive** (accepts strictly more inputs); the risk is over-triggering elsewhere — covered by AC 5 regression checks. Verify `russian_dates.py` re-exports `date_parser`'s function so the change lands in one place.
- **Conventions:** pure deterministic parsers (no I/O, inject `now`/`project_tz`); ruff line-100; 100% coverage.

### Project Structure Notes

- Parser changes are additive and confined to two pure-function modules; the answerer change is the existing `confirm_start` thread from 12.19. No data files, no schema.

### References

- [Source: services/api/app/calendar/service_resolver.py#extract_requested_start] (conservative-by-design docstring)
- [Source: services/api/app/sales/date_parser.py#parse_russian_date_span]
- [Source: services/api/app/calendar/requested_time_check.py#RequestedAvailability] (free verdict carries no datetime → name from the parsed `requested_start`)
- Depends on: Story 12.19.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–6).** `extract_requested_start` now parses a **bare hour** ("в 8" → 08:00; "в 8 утра"/"в 8 вечера"/"ночи"/"дня" disambiguated; out-of-range → `None`) via a new `_HH_BARE` branch in `_extract_clock` + `_apply_period`, and an **ordinal day-of-month** ("31-ое"/"31-го"/"10-е"/"10 числа") via `_extract_ordinal_date`, which rolls forward to the next month whose calendar has that day. Day resolution was factored into `_extract_target_date` (relative/weekday offset OR ordinal absolute). `parse_russian_date_span` gained the same ordinal recognition so a counter-offer is detected without a month name.
- **PITCHING autonomous re-verify (AC 4).** `_handle_pitching` branch (a) now checks the restated time against the calendar: free → `_confirm_slot` names it (`PITCHING_ACCEPT_CONFIRM_LINE`) and closes; busy → `_propose_alternative_or_handoff` offers (and remembers) a fresh alternative; no concrete time / no calendar → falls through to `_complete_booking` (ask / handoff). New `_requested_start_for` re-parses the concrete start for naming.
- **Reported case:** "давайте на 31-ое в 8" now parses → re-verifies 31 May 08:00 → confirms "31 мая на 08:00". The 12.19 reported-phrase test was updated from acceptance-of-offered to the re-verify routing (same user-visible outcome); the free counter-offer test now asserts the named confirmation.
- **Blast radius:** parser changes are additive; `CalendarAvailabilityAnswerer`, the date proposer, and the proactive-followup job stay green.
- **Ambiguity:** bare hour defaults to the literal 24h value (matches how slots are offered); mitigated by naming the slot back to the customer + operator finalisation.
- ruff clean; full suite **3047 passed at 100% coverage**.

### File List

- `services/api/app/calendar/service_resolver.py` (modified — `_HH_BARE`, `_ORDINAL_DAY`, `_apply_period`, bare-hour branch in `_extract_clock`, `_extract_target_date` / `_extract_ordinal_date` / `_ordinal_safe_date`)
- `services/api/app/sales/date_parser.py` (modified — `_ORDINAL_DAY_RE` + ordinal branch)
- `services/api/app/sales/sales_persona_answerer.py` (modified — `_handle_pitching` re-verify branch, new `_requested_start_for`)
- `tests/test_calendar_service_resolver.py` (modified — bare-hour + ordinal cases)
- `tests/test_sales_date_parser.py` (modified — ordinal cases)
- `tests/test_sales_persona_answerer_pitching.py` (modified — free counter-offer now named)
- `tests/test_sales_persona_answerer_pitching_reask_guard.py` (modified — re-verify routing + `_requested_start_for` guard)
