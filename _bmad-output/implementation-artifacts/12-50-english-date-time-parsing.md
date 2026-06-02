# Story 12.50: English date/time reaches the busy check (round-11 R11-2, P1)

Status: review

## Story

As an **English-speaking customer naming a concrete time** ("tomorrow at 2pm"), I want **that slot checked against the calendar**, so that **a busy slot is declined, not silently accepted.**

**Problem (live, round 11, 2 Jun 2026):** "I'd like to book a buggy tomorrow at 2pm for 4 people" (3 June 14:00 = busy) → generic handoff, no «занято»; "What about 10am?" (free) → no verdict either. The English path always handed off without a calendar check.

## Root cause (CONFIRMED)

`services/api/app/calendar/service_resolver.py#extract_requested_start` is Russian-only. Confirmed by running the deployed function: `"tomorrow at 2pm"`, `"2pm"`, `"10am"`, `"June 4 at 10am"` all return `None`, while `"завтра в 14:00"` resolves. With no `requested_start`, the early/pitching busy check is skipped and every English booking hands off. (This also fed round-11 N3 and the English half of R11-1.)

## Fix

Add conservative English support to `extract_requested_start`, mirroring the Russian shapes, so an English booking yields the SAME tz-aware datetime and runs the SAME busy check:

- **Relative days** (raw lowercased text, since English can't be lemmatized): `today` → 0, `tomorrow` → 1, `day after tomorrow` → 2 (checked before `tomorrow`).
- **Weekdays:** `monday`..`sunday` → next occurrence.
- **am/pm clock** (`_AMPM_RE`, checked before `HH:MM` so "2:30pm" reads as 14:30): "2pm" → 14:00, "10am" → 10:00, "12pm" → 12:00, "12am" → 00:00; an out-of-range 12-hour value (e.g. "14pm") declines.
- **`<month> <day>` / `<day> <month>`** ("June 7", "7th of June"): English month prefix table (mirrors the Russian one), optional ordinal + "of"; year rolls forward when the bare day/month already passed.

## Acceptance Criteria

1. English relative/clock/weekday/`<month> <day>` forms resolve to the same datetime as their Russian equivalents. ✅
2. "…tomorrow at 2pm…" (busy) → occupied verdict + nearest-free; "…10am…" (free) → confirmed. ✅ (extractor unblocks the existing busy check)
3. Works on the early/underscoped path, not only full completion. ✅ (it's the shared `extract_requested_start`)
4. Russian conversations unchanged; gates green; 100% coverage. ✅

## Tasks / Subtasks

- [x] English relative-day + weekday matching in `_extract_day_offset` (raw text); am/pm in `_extract_clock` (factored `_ampm_to_hm`); English month table + `_extract_en_absolute_date`; wire into `extract_requested_start`.
- [x] Tests (TDD): tomorrow/today/day-after, am/pm + minutes + noon/midnight edges + invalid hour, weekday next-occurrence, month-day both orders + ordinal/of + past-year roll, EN-time-without-day → None, 24h-with-EN-day.

## Dev Notes

- English relative/weekday words are matched on the raw lowercased text by word boundary (the Russian normalizer can't lemmatize them); Russian still matches on lemmas.
- am/pm is tried BEFORE `_HH_MM` so "2:30pm" isn't misread as 02:30.
- Conservative throughout: still needs BOTH a day anchor and a clock; bare times/fuzzy offsets return `None`.
- **Files:** `services/api/app/calendar/service_resolver.py`.

## References

- Round-11 live QA Defect R11-2 (English sibling of N1). Builds on Story 12.38 (numeric) / 12.33 (weekday).
- [Source: service_resolver.py#extract_requested_start], [#_extract_clock], [#_extract_en_absolute_date].

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–4).** English relative days, weekdays, am/pm clocks and month-day forms now parse to the same tz-aware datetime as Russian, so an English booking reaches the calendar check. TDD; full suite green at 100% coverage. Unblocks R11-1/N3 on the English path.

### File List

- `services/api/app/calendar/service_resolver.py` (modified — English day/clock/month support)
- `tests/test_calendar_service_resolver.py` (modified — English parsing matrix)
- `_bmad-output/implementation-artifacts/12-50-english-date-time-parsing.md` (new)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified)
