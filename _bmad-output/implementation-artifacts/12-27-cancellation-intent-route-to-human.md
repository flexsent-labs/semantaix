# Story 12.27: Cancellation request is its own intent — route to a human

Status: review

## Story

As a **customer who wants to cancel a booking** ("хочу отменить запись", "можно отменить?"),
I want the bot to **acknowledge the cancellation and hand me to a person**,
so that **I'm not pushed back into the booking funnel or given a generic "I don't understand" handoff when I'm trying to cancel**.

**Problem (observed live, багги, 31 May 2026, "Анна Иванова"):**

```
Customer: как отменить бронь?
Анна:     <generic "передам коллегам" handoff / re-asks a scoping field>
```

**Root cause** (`services/api/app/sales/`): there was **no cancellation intent anywhere** in the sales stack — the lemma `отменить` matched nothing in the intent gate, the per-turn classifier (`turn_intent.py`), or any turn-outcome handler. Worse, the booking-positive seeds (`бронь`, `запись`) made `is_sales_intent("хочу отменить запись")` return **True**, pulling cancellations *into* the scoping funnel → the generic handoff. A bare "можно отменить?" (no booking noun) returned `is_sales_intent=False`, so it bypassed the sales answerer entirely and fell through to `GroundedRagAnswerer` → generic HITL. (`followup_cancel_hook.py` is unrelated — it cancels a proactive nudge, not a booking.)

**Decision (operator sign-off, 31 May 2026):** route to a human with a cancellation-specific line + HITL reason; do NOT auto-cancel (no booking-of-record exists — operators finalize bookings); suppress the +24h nudge.

## Acceptance Criteria

1. **Booking-noun cancellation escalates.** `"хочу отменить запись"` (state=None) → `RESPONSE_MODE_SALES_ESCALATION`, `text=CANCELLATION_HANDOFF_LINE`, `hitl_reason='sales_cancellation_request'`, `escalate=True`, `sales_turn_kind='cancellation_request'` — NOT a scoping question. The greeting LLM is never called (caught before it).
2. **Bare cancellation escalates.** `"можно отменить?"` (no booking noun → `is_sales_intent` False) still routes to the same cancellation escalation rather than falling through to RAG.
3. **Mid-funnel cancellation escalates.** A cancellation during `scoping` is caught (not swallowed by the funnel); the stage is parked at the terminal `closing` (a human owns it).
4. **Operator context tagged.** The escalation context marks the ticket as a cancellation ("Запрос на отмену брони") so the operator DM reads `[Запрос на отмену брони] <verbatim message>`.
5. **+24h nudge suppressed.** No follow-up is re-enqueued on a cancellation turn (`suppress_followup`), while a normal booking turn still enqueues exactly one.
6. **Detector is precise.** `is_cancellation` is True for отменить/отменять/отмена forms and False for new bookings (`хочу забронировать багги`), field declines (`без водителей`), price asks, times, and acceptances — so it never swallows another intent.
7. **Gates green.** `ruff check .` clean; full suite at 100% coverage on `platform_common/` + `services/`.

## Tasks / Subtasks

- [x] **Detector `cancel_intent.is_cancellation`** (AC 6) — new `services/api/app/sales/cancel_intent.py`: lemma-based (sibling of `decline.py`), matched on `{отменить, отменять, отмена}` only — narrow on purpose so бронь/запись and field declines are never matched.
- [x] **Dispatch gate** (AC 1, 2, 3) — in `_dispatch`, after loading state and before the `state is None` / not-sales-intent branch, route `is_cancellation` to `_handle_cancellation`. Fires in any state/stage, regardless of `is_sales_intent`.
- [x] **Handler `_handle_cancellation`** (AC 1, 3, 4) — acknowledges with `CANCELLATION_HANDOFF_LINE`, persists `STAGE_CLOSING`, returns a `sales_escalation` result with `hitl_reason=HITL_REASON_CANCELLATION`, `escalation_context=CANCELLATION_ESCALATION_CONTEXT`, `suppress_followup=True`. Plugs into the existing generic `_dispatch_sales_escalation` (ticket create/assign/notify, coalesce into an active ticket).
- [x] **Suppress the nudge** (AC 5) — `try_answer` skips `_enqueue_followup` when `metadata["suppress_followup"]` is set.
- [x] **TDD tests** (AC 1–6) — `tests/test_sales_cancel_intent.py` (detector) + `tests/test_sales_persona_answerer_cancellation.py` (routing, bare/booking/mid-scoping, context tag, nudge suppression + control).
- [x] **Gates** (AC 7) — `ruff` clean; 736 sales/inbound/pipeline/followup tests green.

## Dev Notes

- **Files:** `services/api/app/sales/cancel_intent.py` (new); `services/api/app/sales/sales_persona_answerer.py` (import, `HITL_REASON_CANCELLATION` / `CANCELLATION_HANDOFF_LINE` / `CANCELLATION_ESCALATION_CONTEXT`, dispatch gate, `_handle_cancellation`, follow-up suppression); two new test files.
- **Reuse, don't reinvent:** routes through the existing `_dispatch_sales_escalation` (`main.py:2159`) — same ticket create/assign/notify path as price-unknown / closing handoffs, including the coalesce-into-active-ticket behaviour (a cancellation while a booking ticket is open is added to that ticket, which is the desired operator UX).
- **Why route, not auto-cancel:** bookings are operator-finalized; there is no autonomous booking-of-record to delete. The safe, honest behaviour is acknowledge + escalate. The customer line ("Передам вашу просьбу об отмене коллеге — свяжутся с вами.") does not presume the cancellation is done — consistent with the "no presumed confirmation" copy rule.
- **Why caught first in `_dispatch`:** a cancellation must beat both the funnel (бронь/запись seeds) and the not-sales-intent skip. Gating before both is the only place that catches both live forks with one check.
- **Why `STAGE_CLOSING`:** terminal handoff — a human owns the conversation. Story 12.23 already re-greets a `closing` chat on a fresh booking intent, so a customer who later wants to re-book is not stuck.
- **Why narrow seeds:** the investigation flagged over-broadening as the main risk (collision with `decline.py`'s field-decline detection and with new-booking intent). `{отменить, отменять, отмена}` catches every live utterance without touching another intent — verified by the detector's negative cases.
- **Nudge suppression is sufficient, not just defensive:** the inbound route already calls `followup_cancel_hook.maybe_cancel` before the pipeline, so any *pending* nudge is cancelled when the customer replies; this story only prevents *re-enqueuing* a new one for the cancellation turn.
- **Conventions:** dispatch never raises; immutable `Intent`; `from __future__ import annotations`; ruff E/F/I line-100; 100% coverage gate.

### References

- Story 12.08 (proactive follow-up) / 12.23 (reset stale closing on new booking) — the nudge + closing-stage semantics this leans on.
- Investigation: `_bmad-output/implementation-artifacts/investigations/booking-dialog-bugs-investigation.md` (Finding 1).
