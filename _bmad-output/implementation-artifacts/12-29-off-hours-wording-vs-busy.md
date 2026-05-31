# Story 12.29: Off-hours / closed-day / past requests must not say "занято"

Status: review

## Story

As a **customer asking to book at a time the business is simply closed** (e.g. 23:00, a holiday, a day the service doesn't run),
I want the bot to **tell me the real reason — "вне рабочих часов" — instead of claiming the slot is "занято" (busy)**,
so that **I'm not misled into thinking someone else took the slot when in fact the business isn't open then**.

**Problem (observed live, багги, 31 May 2026, "Анна Иванова"):**

```
Customer: можно в 23:00?
Анна:     К сожалению, это время уже занято. Ближайшее свободное время — …
```

23:00 is outside the operator's working hours, not booked by another customer. "занято" is a falsehood.

**Root cause** (`services/api/app/sales/sales_persona_answerer.py`): the pure availability engine already distinguishes *why* a slot is unavailable — `compute_availability` returns `outside_working_hours` / `wrong_service_day` / `date_exception` / `in_past` / `busy` in a stable precedence (`calendar/availability.py:14-18`) — and `RequestedAvailability.reason` carries it up faithfully (`calendar/requested_time_check.py:172-174`). But both consumers in the answerer (`_complete_booking:1128`, `_maybe_intercept_busy_slot:1443`) branched only on `status`, collapsing every `STATUS_UNAVAILABLE` into `_propose_alternative_or_handoff`, which unconditionally prepended `SLOT_BUSY_LINE`. The granular reason was computed, preserved, then discarded at the presentation layer — mislabeling off-hours, closed-days, wrong-service-days, and past times all as "занято".

## Acceptance Criteria

1. **Off-hours → off-hours line.** A request at 23:00 (outside the 09:00–20:00 window) with nothing booked → reply leads with `SLOT_OFF_HOURS_LINE` ("…вне рабочих часов."), NOT `SLOT_BUSY_LINE`, and still offers the nearest in-hours slot (09:00).
2. **Wrong service day → unavailable line.** A request on a day the service doesn't run (service_days = mon–fri, asked for Saturday) → `SLOT_WRONG_DAY_LINE` ("…в этот день услуга недоступна."), not "занято".
3. **Closed date → closed line.** A request on an explicit `date_exception` (or resolved public holiday) → `SLOT_CLOSED_DATE_LINE` ("…в этот день мы не работаем."), not "занято".
4. **Past time → past line.** A request earlier today (10:00 when it's 12:00) → `SLOT_IN_PAST_LINE` ("…это время уже прошло."), not "занято".
5. **Genuinely busy → "занято" unchanged.** A real conflicting `BusyInterval` at an in-hours time still leads with `SLOT_BUSY_LINE`. Unmapped reasons (`busy`, the rare `outside_lookahead`, `None`) default to `SLOT_BUSY_LINE`.
6. **No other behavior changes.** The alternative-offer / handoff tail, stage transition (`→ pitching`), `last_proposal`, escalation contract (Story 12.22), and `sales_turn_kind` metadata are all unchanged — only the lead line differs.
7. **Gates green.** `ruff check .` clean; full suite at 100% coverage on `platform_common/` + `services/`.

## Tasks / Subtasks

- [x] **Reason→line mapping** (AC 1–5) — import `REASON_OUTSIDE_WORKING_HOURS` / `REASON_WRONG_SERVICE_DAY` / `REASON_DATE_EXCEPTION` / `REASON_IN_PAST` from `calendar/availability.py`; add `SLOT_OFF_HOURS_LINE` / `SLOT_WRONG_DAY_LINE` / `SLOT_CLOSED_DATE_LINE` / `SLOT_IN_PAST_LINE` constants and a `_UNAVAILABLE_LEAD_LINES` dict.
- [x] **Thread the reason** (AC 1–6) — add a `reason: str | None = None` parameter to `_propose_alternative_or_handoff`; select the lead line via `_UNAVAILABLE_LEAD_LINES.get(reason, SLOT_BUSY_LINE)`. Pass `requested.reason` from `_complete_booking` and `availability.reason` from `_maybe_intercept_busy_slot`.
- [x] **TDD tests** (AC 1–5) — new `tests/test_sales_persona_answerer_off_hours_wording.py`: off-hours, wrong-service-day, closed-date, past-time each assert the specific line + absence of `SLOT_BUSY_LINE`; a genuinely-busy regression keeps `SLOT_BUSY_LINE`.
- [x] **Update the mislabeled 12.22 test** (AC 5, 6) — `test_requested_time_..._no_alternative_hands_off` used a no-working-hours rule (reason = `wrong_service_day`) but asserted "занято"; now asserts `SLOT_WRONG_DAY_LINE` while keeping the no-alternative escalation contract.
- [x] **Gates** (AC 7) — `ruff` clean; 209 sales/calendar tests green.

## Dev Notes

- **Files:** `services/api/app/sales/sales_persona_answerer.py` (imports, 4 new constants + `_UNAVAILABLE_LEAD_LINES`, `_propose_alternative_or_handoff` signature + lead-line selection, 2 call sites); `tests/test_sales_persona_answerer_off_hours_wording.py` (new); `tests/test_sales_persona_answerer_pitching.py` (updated mislabeled test).
- **Reuse, don't reinvent:** the reason constants are the engine's single source (`availability.py:38-43`); the answerer now imports them directly rather than re-deriving. `find_earliest_slot` already returns an in-hours slot, so the "Ближайшее свободное время" tail composes naturally with every reason.
- **Why default to `SLOT_BUSY_LINE`:** `busy` is the common case and the safest fallback for any future/rare reason (`outside_lookahead`). The mapping is additive — unmapped reasons read exactly as before.
- **Why `turn_kind` is unchanged:** `scoping_complete_busy_alternative` / `_no_slot` are observability keys asserted by existing tests; the customer-facing line is the only contract that changed. Keeping them avoids churn and preserves the Story 12.22/12.25 metadata surface.
- **Broader blast radius fixed:** the same collapse mislabeled `wrong_service_day`, `date_exception`, and `in_past` too — all corrected here in one mapping, per the investigation's "fix the whole class" recommendation.
- **Conventions:** answerer dispatch never raises; immutable `Intent`; time injected; `from __future__ import annotations`; ruff E/F/I line-100; 100% coverage gate.

### References

- Story 12.22 (defer HITL until alternative accepted) / 12.25 (early busy-check) — the two busy-check call sites threaded here.
- Engine reasons + tests: `services/api/app/calendar/availability.py`, `tests/test_calendar_availability.py`.
- Investigation: `_bmad-output/implementation-artifacts/investigations/booking-dialog-bugs-investigation.md` (Finding 3).
