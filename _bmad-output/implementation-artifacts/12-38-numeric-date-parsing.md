# Story 12.38: Numeric / ISO / slash date parsing in the busy check (D10 #30, P2)

Status: review

## Story

As a **customer who states a booking date numerically** ("20.06 в 13:00", "01.06.2026", "2026-09-15", "20/06"),
I want the bot to **check that slot against the calendar like it does for "20 июня"**,
so that **a numeric date isn't silently skipped (handed off unverified) just because I didn't spell the month in Russian.**

**Problem (round-5 QA #30; confirmed by live probe in the validation investigation):**

`extract_requested_start` (the single busy-check entrypoint shared by the sales persona and the legacy calendar answerer) only recognised a day anchor as a relative word, a weekday, or a Russian `"<day> <month>"` word date. Numeric forms returned `None` and skipped the calendar check entirely. Two coupled root causes:

1. `_ABS_DATE_RE` requires a **Cyrillic month word** + numeric day, so `20.06`, `01.06.2026`, ISO `2026-09-15`, and slash `20/06` never matched.
2. The clock regex `_HH_MM = \b(\d{1,2})[:.](\d{2})\b` **greedily ate** a dotted `DD.MM` as a `HH.MM` time — e.g. in "20.06 в 13:00" it read `20.06` → `20:06` and ignored the real `13:00`, so even the clock was wrong.

Mitigation already in place: Story 12.32 means an unverifiable slot is **never silently accepted** — it hands off honestly as "unverified". So this is a **coverage refinement** (verify more slot formats automatically), not a silent-accept correctness hole.

## Acceptance Criteria

1. A dotted `DD.MM` paired with a clock parses tz-aware and reaches the busy check: `"20.06 в 13:00"` → `2026-06-20 13:00` (with `now=2026-06-05`).
2. Year handling matches the Russian-word path: an **inferred** year rolls to next year when the bare day/month has lapsed (`"02.06"` → 2027); an **explicit** year is honored verbatim even if past (`"01.06.2026"` → 2026-06-01).
3. ISO `YYYY-MM-DD`, slash `DD/MM(/YYYY)`, and dotted-with-year `DD.MM.YYYY`/`DD.MM.YY` parse too.
4. The `час`-form clock still resolves after a numeric date (`"20.06 в 13 часов"` → 13:00).
5. **Ambiguity preserved:** a bare dotted token with no *separate* clock stays a TIME, not a date — `"завтра в 05.06"` → tomorrow 05:06 (not "5 June"); `"завтра в 15.30"` (existing test) still → 15:30.
6. **Relative anchor still wins** over a numeric date, and the numeric span is stripped so the clock is read correctly: `"завтра, не 20.06, в 10:00"` → tomorrow 10:00 (not 20:06).
7. Invalid numeric dates (`31.02`, `40/06`, ISO month 13) and a date without any clock return `None` (conservative — clarify/escalate, never guess). Gates green; 100% coverage.

## Design — why this is safe

The fix extracts a numeric date **first**, records its `(start, end)` span, and **strips that span before clock extraction**. That single move fixes the greedy-eat bug for every format at once.

The only genuinely ambiguous form is a bare dotted `DD.MM` (collides with `HH.MM`). Disambiguation rule: **a dotted `DD.MM` is a date only if removing it still leaves a parseable clock.** This is exactly the booking shape ("20.06 в 13:00" has a separate "13:00") and preserves every existing dotted-time test ("завтра в 15.30" has no other clock → stays a time). ISO / slash / dotted-with-year carry a year or a non-`.` separator and are unambiguous, so they're matched first and unconditionally.

## Tasks / Subtasks

- [x] `_extract_numeric_date(text, today)` → `(date, span) | None`: ISO, dotted-with-year, slash, then conditional bare-dotted (accept iff stripping leaves a clock).
- [x] `_resolve_dated(...)`: explicit year honored (2-digit → 2000+); inferred year rolls forward when lapsed — mirrors `_extract_absolute_date`.
- [x] `_strip_span(...)`: blank the matched date span (space, not empty, so tokens don't merge).
- [x] `extract_requested_start`: locate numeric date up front; relative offset still wins; strip the numeric span from the clock source in every branch.
- [x] Tests (TDD): 16 new cases — each format, both year modes, час-form, ambiguity preservation, relative-wins, and the invalid/no-time guards.

## Dev Notes

- **Single chokepoint:** `extract_requested_start` is the only date/time extractor used by both `services/api/app/calendar/availability_answerer.py:180` and `services/api/app/sales/sales_persona_answerer.py` (×4), so the fix lands end-to-end in one place.
- **Scope held deliberately narrow:** ordinal words ("первого июня") are *not* added here — they belong to the Russian-word path (`_extract_absolute_date`) and are lower value (the LLM rarely emits them). Tracked separately if they recur.
- **Files:** `services/api/app/calendar/service_resolver.py`.

## References

- Investigation: `_bmad-output/implementation-artifacts/investigations/booking-dialog-defects-validation-investigation.md` — D2-numeric-dates (rec. #3: "extend `_extract_absolute_date` for dotted/ISO/slash … and stop `_HH_MM` from eating `01.06`").
- Round-5 QA defect #30 (numeric date booking).
- Mitigation context: Story 12.32 (never silently accept an unverifiable slot).
- [Source: services/api/app/calendar/service_resolver.py#extract_requested_start].

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–7).** `extract_requested_start` now parses numeric/ISO/slash dates and fixes the `_HH_MM` greedy-eat by stripping the date span before clock extraction. The bare-dotted ambiguity is resolved by the "stripping leaves a clock" rule, preserving every existing dotted-time test.
- **End-to-end:** both the legacy `CalendarAvailabilityAnswerer` and the sales persona busy-check (12.26/12.32) pick up the new formats with no call-site change.
- **TDD/regression:** 16 new tests, all watched RED first (incl. the smoking-gun `"завтра, не 20.06, в 10:00"` → `20:06` greedy-eat). `service_resolver.py` 100% coverage; full suite green.

### File List

- `services/api/app/calendar/service_resolver.py` (modified)
- `tests/test_calendar_service_resolver.py` (modified — 16 numeric-date tests)
- `_bmad-output/implementation-artifacts/12-38-numeric-date-parsing.md` (new)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified)
