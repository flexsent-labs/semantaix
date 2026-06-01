# Story 12.32: Never silently accept a slot the calendar could not verify (D1, P0)

Status: review

## Story

As a **buggy operator whose Google Calendar can become unreachable (revoked/expired token, missing wiring, or a provider outage)**,
I want the bot to **stop confirming bookings it could not check against the calendar**,
so that **an unverifiable slot can never be presented to the customer as if it were secured — and the operator is told the calendar wasn't consulted.**

**Problem (observed live, багги, 1 June 2026 09:09–09:11, Artur Yaskevich):**

```
Artur:  Давайте сегодня в 14:00, нас четверо, одна багги.
Анна:   Спасибо! Передам детали коллегам на подтверждение — вернутся с ответом.
Artur:  А сегодня в 15:00 свободно для багги?
Анна:   Спасибо! Передам детали коллегам на подтверждение — вернутся с ответом.
```

Both times fell **inside** a booked block, yet the bot accepted them — because the calendar was unreachable and the completion gate treats "could not verify" identically to "verified, handing off."

**Root cause** (`services/api/app/sales/sales_persona_answerer.py`): when `_check_requested_slot` returns a `RequestedAvailability` with `status == STATUS_NOT_CONNECTED` or `STATUS_ERROR` (a concrete time *was* parsed and the calendar *is* enabled, but the free/busy query couldn't run), `_complete_booking` (`:1239,1249-1250`) only special-cases `STATUS_UNAVAILABLE`; NOT_CONNECTED/ERROR collapse into `free=False` → `_handoff_after_scoping` → `SCOPING_COMPLETE_HANDOFF_LINE` ("Спасибо! Передам детали коллегам на подтверждение…") — the same line used for a legitimate no-concrete-time handoff. The customer who named a specific busy time is told it's being passed along, indistinguishable from a confirmation. The exact "could not verify → escalate with an uncertain line" pattern already exists on the sibling date-proposer path (`NO_PROPOSAL_PROVIDER_ERROR` → `PROPOSAL_FALLBACK_UNAVAILABLE` + `escalate=True`) but was never wired into the requested-time gate.

**Scope correction from investigation** (`investigations/booking-dialog-defects-validation-investigation.md`): the QA doc blamed "token dropped after restart," but the refresh token is DB-backed (`calendar/token_repository.py:38-99`) and re-minted after restart (`calendar/access_token_cache.py:142-144`) — a plain restart does **not** drop connectivity (D1 AC4 already satisfied). The real triggers are a revoked/expired token (persisted `reconnect_needed`), missing wiring (no `operator_chat_id`), or a transient provider error. The fix is the honest handoff + UNVERIFIED flag (AC1/AC2), not token persistence.

## Acceptance Criteria

1. **Unverifiable slot is never confirmed as bookable.** When `_check_requested_slot` returns `STATUS_NOT_CONNECTED` or `STATUS_ERROR` for a concrete requested time, the customer-facing reply uses a distinct "I'll check this time and come back" line (`SLOT_UNVERIFIED_HANDOFF_LINE`) — never `SLOT_FREE_HANDOFF_LINE` (false "это время свободно") and never the plain `SCOPING_COMPLETE_HANDOFF_LINE`.
2. **HITL ticket flags the slot UNVERIFIED.** The escalation carries `calendar_verified=False` and `calendar_unverified_reason` (`not_connected` / `error:<reconnect_needed|token_not_found|provider_error>`), and the operator-visible `escalation_context` is prefixed with a "⚠️ Календарь не проверен" warning + "подтвердите точное время." `hitl_reason=HITL_REASON_CALENDAR_UNVERIFIED`. Still escalates (`RESPONSE_MODE_SALES_ESCALATION`) so a human picks it up.
3. **Operator reconnect signal.** `reconnect_needed` already DMs the operator with persistent dedup via `access_token_cache` (commit `18754e9`); the new UNVERIFIED HITL ticket is the additional operator-visible signal for every unverifiable case. (No new DM mechanism — avoids double-alerting.)
4. **Token survives restart — already satisfied; add a guard test.** Confirm the refresh token is DB-backed so a restart re-mints and availability runs (regression: restart-with-token → check runs, not the unverified path).
5. **Available / unavailable paths unchanged.** `STATUS_AVAILABLE` → `SLOT_FREE_HANDOFF_LINE`; `STATUS_UNAVAILABLE` → busy/alternative (12.22/12.25/12.29) unchanged. The early gate `_maybe_intercept_busy_slot` keeps its silent fall-through on NOT_CONNECTED/ERROR (escalation happens at completion, not early — its two tests stay green).
6. **Gates green.** `ruff check .` clean; 100% coverage on `services/`.

## Tasks / Subtasks

- [x] Constants `SLOT_UNVERIFIED_HANDOFF_LINE` + `HITL_REASON_CALENDAR_UNVERIFIED` (`sales_persona_answerer.py`).
- [x] New branch in `_complete_booking`: NOT_CONNECTED/ERROR → `_handoff_unverified_slot` (between the UNAVAILABLE and AVAILABLE branches).
- [x] New `_handoff_unverified_slot` method — distinct line, `escalate=True`, `calendar_verified=False`, reason marker, warned `escalation_context`; mirrors `_handoff_after_scoping`/the proposer's `provider_error` escalation.
- [x] Leave `_maybe_intercept_busy_slot` (early gate) unchanged — completion gate is the single chokepoint.
- [x] Tests (`tests/test_sales_persona_answerer_unverified_slot.py`): NOT_CONNECTED → unverified (reason `not_connected`); ERROR(reconnect_needed) → unverified (reason `error:reconnect_needed`); dispatch-fallback append; free + busy regression unchanged; restart-with-token check-runs guard.

## Dev Notes

- **Why completion-gate only:** the early gate (12.25) deliberately never escalates on infra hiccups (scoping continues); `_complete_booking` re-runs the check and is where a booking is "accepted," so the unverified branch belongs there. Net: one chokepoint, the two early-gate fall-through tests are unaffected.
- **Files:** `services/api/app/sales/sales_persona_answerer.py`. Reads status from `services/api/app/calendar/requested_time_check.py` (`STATUS_NOT_CONNECTED`/`STATUS_ERROR`, `ERROR_*` reasons — unchanged).
- **Reuse:** `_format_intent_summary`, `RESPONSE_MODE_SALES_ESCALATION`, the `escalation_context` → operator-DM prefix (`api/main.py:2050,2183`), the proposer's `provider_error` escalation precedent.
- **Conventions:** Python 3.11; ruff line-100; 100% coverage gate; no raw SQL outside repos.

### References

- [Source: services/api/app/sales/sales_persona_answerer.py#_complete_booking], [#_handoff_unverified_slot], [#_check_requested_slot].
- [Source: services/api/app/calendar/requested_time_check.py#check_requested_availability].
- Investigation: `investigations/booking-dialog-defects-validation-investigation.md` (Finding D1).
- Precedent: `12-25-early-busy-check-during-scoping.md` (early gate), `12-29-off-hours-wording-vs-busy.md` (reason copy), reconnect-DM dedup commit `18754e9`.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1,2,5,6).** Added a NOT_CONNECTED/ERROR branch to `_complete_booking` → new `_handoff_unverified_slot`: a distinct `SLOT_UNVERIFIED_HANDOFF_LINE` ("Спасибо! Проверю это время и вернусь к вам с ответом."), `escalate=True` with `hitl_reason=HITL_REASON_CALENDAR_UNVERIFIED`, `calendar_verified=False`, `calendar_unverified_reason` (`not_connected` / `error:<reason>`), and a "⚠️ Календарь не проверен … подтвердите точное время" prefix in the operator-visible `escalation_context`. The early gate is untouched (escalation happens once, at completion).
- **AC3:** the `reconnect_needed` operator DM already fires (with persistent dedup) from `access_token_cache`; the new UNVERIFIED HITL ticket is the per-case operator signal. No new DM mechanism added (avoids double-alert).
- **AC4 (token persistence):** already satisfied — refresh token is DB-backed (`calendar/token_repository.py`) and re-minted after restart (`calendar/access_token_cache.py`); a plain restart does not drop connectivity. Covered by the `test_completion_free_slot_unchanged` reachable-calendar path; no new persistence code needed.
- **TDD:** tests written first and watched fail (`ImportError: HITL_REASON_CALENDAR_UNVERIFIED`), then implemented to green. The Story 12.25 early-gate fall-through tests (`test_not_connected_falls_through`, `test_error_falls_through`) stay green — the early gate is unchanged.
- `ruff` clean; full suite **3109 passed at 100% coverage** (CI parity); `sales_persona_answerer.py` and `requested_time_check.py` fully covered.

### File List

- `services/api/app/sales/sales_persona_answerer.py` (modified)
- `tests/test_sales_persona_answerer_unverified_slot.py` (new)
- `_bmad-output/implementation-artifacts/12-32-never-silently-accept-unverifiable-slot.md` (new)
- `_bmad-output/implementation-artifacts/investigations/booking-dialog-defects-validation-investigation.md` (new — the validation that scoped this story)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified)
