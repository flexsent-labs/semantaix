# Story 12.37: Legacy availability answerer defers unresolved sales bookings (D11, P2)

Status: review

## Story

As a **customer who states the service, date and time of a booking**,
I want the bot to **not re-ask "на какую услугу и на какое время"**,
so that **a clear buggy booking isn't bounced by the legacy availability answerer when the sales persona briefly yields.**

**Problem (observed live, round 5, 1 June 2026 16:30):**

```
Запишите на багги 3 июня в 13:00, нас четверо, одна багги.
→ Подскажите, пожалуйста, на какую услугу и на какое время вы хотите записаться?   ❌ (D11)
```

**Root cause (CONFIRMED — round-5 investigation):** the `SalesPersonaAnswerer` is first in the pipeline but, per its deliberate Story 12.09 "thin gate" contract, it `_skip`s on a transient `complete_json` failure (the disk-full window). The yielded sales-intent booking then reaches `CalendarAvailabilityAnswerer`, which has **no sales gate** and whose `resolve_service` can't match the project's multi-word service name → it emits `CLARIFY_NO_SERVICE` even though the customer named both the service and the time.

## Acceptance Criteria

1. A **sales-intent** turn whose service `CalendarAvailabilityAnswerer` can't resolve (`NoMatch`) **defers** (skip) to the `SalesPersonaAnswerer` instead of emitting `CLARIFY_NO_SERVICE` — the yielded turn then reaches the inbound HITL escalation.
2. A `NoMatch` on a **non-sales** scheduling ask ("запишите меня в субботу в 15:00", no service word) still clarifies as before.
3. A sales turn that **does** resolve still gets a real availability verdict (resilience preserved when the persona LLM is briefly down).
4. Story 12.09 (persona "thin gate") unchanged; gates green; 100% coverage.

## Out of scope (documented follow-ups)

- **D10 (ScopeGuard declines an in-scope price/booking ask, "Этим не занимаюсь."):** the obvious fix — gate `ScopeGuardAnswerer` on `is_sales_intent` — is **unsafe**: `is_sales_intent` has false positives (e.g. `is_sales_intent("Какое сегодня число?") == True`), so it would make datetime/factual questions that reach the guard *escalate to HITL* instead of declining (broke 3 e2e decline tests, incl. a datetime question). D10 needs a **precise** in-scope signal (not the loose sales-intent seed match) and is a degraded-case symptom (in normal operation #28 is answered by Story 12.28); deferred for a dedicated story rather than shipping an imprecise gate. D12's pipeline timeout (#117) already covers the hung-pipeline silence.
- **D10 #30 numeric `DD.MM` date parsing** → separate story 12.38.

## Tasks / Subtasks

- [x] `CalendarAvailabilityAnswerer.try_answer`: on `NoMatch`, defer (skip) when `is_sales_intent(question)`; non-sales NoMatch still clarifies. (Gated behind the existing `has_scheduling_intent` check, so it isn't exposed to sales-intent false positives on non-scheduling turns.)
- [x] Tests: sales-intent NoMatch → defer (no clarify armed); repointed the existing clarify→escalate test to a non-sales scheduling phrasing ("запишите меня …").

## Dev Notes

- **Why NoMatch-only:** a sales turn that resolves still gets a correct availability verdict (good resilience if the persona LLM is down); only the mis-firing `NoMatch → CLARIFY` defers. This narrowed the blast radius from 12 broken engine tests (gating before `resolve_service`) to 0 + 1 repointed clarify test.
- **Why not gate ScopeGuard for D10:** see Out of scope — `is_sales_intent` is too imprecise to gate the last-resort guard.
- **Files:** `services/api/app/calendar/availability_answerer.py`.

## References

- Investigation: round-5 Finding D11 (legacy answerer claims a yielded sales booking; `resolve_service` mismatch on the multi-word service name).
- [Source: services/api/app/calendar/availability_answerer.py#try_answer NoMatch branch].
- Contract preserved: `12-09` "thin gate"; `test_sales_persona_answerer_llm_schema_violation.py`.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–4, D11).** `CalendarAvailabilityAnswerer` now defers a sales-intent `NoMatch` to the persona instead of `CLARIFY_NO_SERVICE`. Non-sales NoMatch still clarifies; resolvable sales turns still get a verdict. Story 12.09 preserved.
- **Two approaches discarded, with evidence:** (a) making the persona escalate its own skips violated Story 12.09 (broke 9 "thin gate" tests); (b) gating ScopeGuard on `is_sales_intent` for D10 was unsafe (`is_sales_intent` false-positives "Какое сегодня число?" → 3 e2e decline tests broke). Landed the narrow, principled D11 gate only.
- **TDD/regression:** added a sales-intent-NoMatch defer test; repointed one clarify→escalate test to a non-sales phrasing.
- `ruff` clean; full suite green at 100% coverage.

### File List

- `services/api/app/calendar/availability_answerer.py` (modified)
- `tests/test_calendar_availability_answerer.py` (modified — defer test + repointed clarify test)
- `_bmad-output/implementation-artifacts/12-37-sales-owns-inscope-turns.md` (new)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified)
