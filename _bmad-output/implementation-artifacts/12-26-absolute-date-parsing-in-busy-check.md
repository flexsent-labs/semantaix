# Story 12.26: Absolute "<day> <month>" date parsing reaches the busy check

Status: review

## Story

As a **customer who names a concrete calendar date and time** (e.g. "2 июня в 11:00"),
I want the bot to **check that slot's availability just like it does for "завтра в 11:00"**,
so that **a busy or off-hours slot is never silently accepted because I happened to phrase the date as an absolute one**.

**Problem (observed live, багги, 31 May 2026, "Анна Иванова" persona):**

```
Customer: можно в понедельник в 13:00?      → "передам коллегам" (ACCEPTED — but 13:00 was busy)
Customer: можно завтра в 13:00?             → занято + offers 08:00 (correctly REJECTED)
Customer: 2 июня в 11:00                     → "уточните дату и время" (date+time given, yet not parsed)
```

The identical request was accepted via one phrasing and rejected via another. Root cause: the scoping LLM frequently *resolves* a relative reference ("в понедельник", even "завтра") into an absolute date like "1 июня" before it reaches the busy check. `extract_requested_start` only parsed relative anchors (`завтра`/`сегодня`/weekday), so an absolute date parsed to `None`, the calendar check in Story 12.25 / 12.22 was silently skipped, and the slot was handed off as bookable.

**Root cause** (`services/api/app/calendar/service_resolver.py`): `extract_requested_start` returned a `datetime` only when `_extract_day_offset` resolved a relative/weekday anchor. An explicit `<day> <month>` date had no parse branch → `None` → `_check_requested_slot` / `_maybe_intercept_busy_slot` short-circuit before `check_requested_availability`.

## Acceptance Criteria

1. **Absolute date + time parses.** "2 июня в 11:00" with `now=2026-06-05` → a tz-aware `datetime` in `project_tz`; because 2 June is already past this year, the year rolls forward to 2027.
2. **Later-this-year absolute date.** "на 15 сентября в 9:00" → `2026-09-15 09:00` (no roll-forward).
3. **Today is not past.** "5 июня в 18:00" with `now=2026-06-05` → `2026-06-05 18:00` (same calendar day is accepted, not rolled forward).
4. **Inflected month resolves.** Genitive "1 июля в 3 часа" → `2026-07-01 03:00` (month matched by prefix; час-form clock).
5. **Absolute date without a time → None.** "2 июня" alone → `None` (a day anchor without a clock never books).
6. **Day + non-month word → None.** "нас 4 человека в 11:00" → `None` (a bare digit followed by a non-month word is not a date).
7. **Invalid absolute day → None.** "31 апреля в 12:00" → `None` (no real 31 April; roll-forward does not salvage it).
8. **Relative anchor still wins.** "завтра, не 20 июня, в 10:00" → relative offset is used (tomorrow), not the absolute date.
9. **Answerer-level regression.** An absolute-date opener whose slot is busy now intercepts with `SLOT_BUSY_LINE` + nearest free slot, identical to the relative-date path (no silent acceptance).
10. **Gates green.** `ruff check .` clean; targeted busy-check + date suites pass; no regressions.

## Tasks / Subtasks

- [x] **`_extract_absolute_date(text, today)`** (AC 1–3, 7) — new helper parsing `\b(\d{1,2})\s+([а-яё]+)` against a month-prefix table (`_MONTH_PREFIXES`); validates via `_safe_date`; rolls the year forward when the bare day/month is already past locally.
- [x] **`_month_from_token` + `_safe_date`** (AC 4, 7) — prefix lookup (one prefix covers every grammatical case) + `ValueError`-guarded `date()` constructor.
- [x] **Teach `extract_requested_start`** (AC 1–8) — when `_extract_day_offset` finds no relative/weekday anchor, fall back to `_extract_absolute_date(text.lower(), local_now.date())`; still require a concrete clock, so bare dates and fuzzy offsets return `None`.
- [x] **TDD tests** (AC 1–9) — 8 unit cases in `tests/test_calendar_service_resolver.py` + 1 answerer-level regression in `tests/test_sales_persona_answerer_early_busy_check.py` proving an absolute date intercepts a busy slot.
- [x] **Gates** (AC 10) — `ruff check .` clean; 40 targeted tests green.

## Dev Notes

- **Files:** `services/api/app/calendar/service_resolver.py` (new `_MONTH_PREFIXES`, `_ABS_DATE_RE`, `_extract_absolute_date`, `_month_from_token`, `_safe_date`; extended `extract_requested_start`); `tests/test_calendar_service_resolver.py`, `tests/test_sales_persona_answerer_early_busy_check.py` (tests).
- **Conservative by design:** mirrors the rest of the extractor — only commits when a day number pairs with a recognisable Russian month word AND a valid clock is present. Month lookup is by prefix so a single entry ("июн") covers июня/июне/июнь.
- **Year roll-forward:** a customer naming "5 января" in December means the upcoming January, not eight months gone. Past-this-year dates roll to `today.year + 1`; today itself is not past.
- **Relative still wins:** `_extract_day_offset` is tried first, so "завтра" alongside a stray absolute date uses the relative offset — the absolute branch is a fallback for LLM-resolved dates only.
- **Origin:** applied from `booking-dialog-busy-check-fix.patch` (bugs A+B from the 31 May 2026 live test); committed here for traceability. Extends Story 12.25's early busy check and Story 12.22's completion-time check, both of which call `extract_requested_start`.

### References

- Story 12.25 (early busy-check during scoping) — the intercept this parsing feeds.
- Investigation: `_bmad-output/implementation-artifacts/investigations/booking-dialog-bugs-investigation.md`.
