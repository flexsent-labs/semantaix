# Story 12.51: Negotiation re-checks the customer's counter-time (round-11 R11-1, P2)

Status: review

## Story

As a **customer who, after a busy verdict, proposes a different time** («а давайте тогда в 12:00»), I want **the bot to check and confirm the time I proposed**, so that **it doesn't silently book the 08:00 slot it suggested.**

**Problem (live, round 11):** busy 14:00 → bot offers 08:00 → customer «а давайте тогда в 12:00» → bot confirms **08:00** (its own slot). Correction «именно в 12:00, а не в 08:00» → generic handoff, still no verdict for 12:00.

## Root cause (CONFIRMED)

Reproduced byte-exact on the deployed class. In `_handle_pitching`: (a) the counter-offer detector (`_merge_dates_from_customer_message`) only adopts a NEW date when a concrete **date+time** parses — a time-only «в 12:00» (date implied by context) parses to `None`, so it isn't a counter-offer; (b) the next branch `is_acceptance(question)` matches the leading «давайте» and confirms the stored 08:00. A counter-proposal is read as acceptance of the bot's slot. The correction matches neither branch (`is_acceptance`=False) → `_handoff_after_pitching_followup`, no 12:00 verdict.

## Fix

Add a **time-only counter-offer** branch in `_handle_pitching`, BEFORE the acceptance check:

- `_timeonly_counteroffer_start` collects every clock time in the message (`extract_all_clocks`, am/pm + HH:MM + «N часов»), **excludes the bot's own offered time**, and — if exactly one new time remains — combines it with the **date of the offered slot** (carried from `last_proposal`). That datetime is canonicalised into `intent.dates` and run through `_complete_booking` → the busy check.
- Excluding the proposal's own time means restating it («давайте в 08:00») stays an acceptance, while a single different time («в 12:00») — even amid a correction naming both («именно в 12:00, а не в 08:00») — is the counter. Ordering counter-before-acceptance stops a leading «давайте»/«ok» from booking the bot's slot.

## Acceptance Criteria

1. After a busy verdict + alternative, a message naming a new time (date inherited from context) re-runs the availability check on the NEW time and confirms/declines THAT time. ✅
2. The bot never books a time the customer didn't choose; the alternative is used only on a bare acceptance. ✅
3. A correction («нет, в 12:00, а не в 08:00») produces a verdict for 12:00, not a generic handoff. ✅
4. Date context carries across turns (from the offered slot). ✅
5. Gates green; 100% coverage. ✅

## Out of scope (follow-up)

- Date carryover uses the offered slot's date (`last_proposal`). A pitching state with no offered slot (e.g. a free-handoff) won't carry a date for a bare time-only reply — rare, deferred.
- The "не Y, а X" ordering is handled by excluding the proposal time (X is the lone non-proposal time); a counter naming two NON-proposal times is treated as ambiguous → handoff.

## Tasks / Subtasks

- [x] `extract_all_clocks` in `service_resolver.py` (am/pm + HH:MM + часов, deduped); factor `_ampm_to_hm`.
- [x] `_pitching_offered_slot` + `_timeonly_counteroffer_start` in the persona; insert the time-only counter branch before acceptance in `_handle_pitching`.
- [x] Tests (TDD): counter «в 12:00» (busy → verdict, not 08:00), correction picks the non-proposal time, free counter confirms 12:00, bare acceptance still confirms 08:00 (no re-check); `extract_all_clocks` unit matrix.

## Dev Notes

- The full date+time counter path (`_merge_dates_from_customer_message`, Story 12.48) is unchanged; this adds only the time-only-with-carryover case, so the existing numeric counter-offer behaviour is preserved.
- Canonicalising the counter time into `intent.dates` as `"%Y-%m-%d %H:%M"` lets the existing busy check re-parse it unambiguously.
- **Files:** `services/api/app/calendar/service_resolver.py`, `services/api/app/sales/sales_persona_answerer.py`.

## References

- Round-11 live QA Defect R11-1. Related: 12.48 (numeric counter-offer), 12.22 (defer HITL until accepted).
- [Source: sales_persona_answerer.py#_handle_pitching], [#_timeonly_counteroffer_start], [service_resolver.py#extract_all_clocks].

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–5).** A time-only counter-offer (date carried from the offered slot, the bot's own time excluded) is re-checked before any acceptance, so «давайте в 12:00» books 12:00 (or declines it), never the offered 08:00. Reproduced the live bug, then fixed it. TDD; full suite green at 100% coverage.

### File List

- `services/api/app/calendar/service_resolver.py` (modified — `extract_all_clocks` + `_ampm_to_hm`)
- `services/api/app/sales/sales_persona_answerer.py` (modified — counter-offer-before-acceptance + helpers)
- `tests/test_sales_persona_answerer_early_busy_check.py` (modified — R11-1 negotiation tests)
- `tests/test_calendar_service_resolver.py` (modified — `extract_all_clocks` matrix)
- `_bmad-output/implementation-artifacts/12-51-pitching-counteroffer-time-carryover.md` (new)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified)
