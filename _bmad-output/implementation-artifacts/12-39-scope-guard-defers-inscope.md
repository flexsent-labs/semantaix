# Story 12.39: Scope guard defers in-scope asks to HITL (D10 ScopeGuard, P2)

Status: review

## Story

As a **customer who asks an in-scope price/booking question**,
I want the bot to **route me to a human when it can't answer**,
so that **a real booking isn't bounced with "Этим не занимаюсь." just because the sales persona briefly failed.**

**Problem (round-5 QA, D10 — degraded window):**

```
Сколько стоит покраска? / Запишите на багги 3 июня в 13:00
→ Этим не занимаюсь.   ❌ (D10 — declined a real in-scope ask)
```

**Root cause (CONFIRMED):** `ScopeGuardAnswerer` is the last-resort answerer — it declines anything the upstream answerers didn't handle, to keep off-topic chatter out of the operator queue. But during the disk-full window the `SalesPersonaAnswerer` `_skip`s on its LLM failure (its deliberate Story 12.09 "thin gate" contract), the message falls past RAG, and ScopeGuard declines a **genuine in-scope** booking/price ask.

This is the third round-5 vector of the same degradation (with D11/#118 and D12/#117). D12's pipeline timeout covers a *hung* pipeline; this covers a *fast LLM error* that falls all the way through.

## Why the obvious fix was unsafe (and what replaced it)

Gating ScopeGuard on `is_sales_intent` is **wrong**: it false-positives on factual questions (`is_sales_intent("Какое сегодня число?") == True`), which would escalate datetime/factual asks instead of declining them (it breaks the `test_e2e_..._scope_decline` datetime test). A probe confirmed a **precise** signal that cleanly separates the cases:

| input | is_sales_intent | PRECISE (sched ∨ price/catalog, ∧ ¬oos) |
|---|---|---|
| Сколько стоит покраска? | 1 | **1** ✅ |
| Запишите на багги 3 июня… | 1 | **1** ✅ |
| Какое сегодня число? | 1 (false +) | **0** ✅ |
| Который час? / погода / анекдот | 0 | **0** ✅ |
| Где забронировать отель? (lodging) | 1 | **0** (excluded by `is_out_of_scope`) ✅ |

## Acceptance Criteria

1. On a **bookings-enabled** project, an in-scope price/booking ask that reaches ScopeGuard **defers** (`handled=False`, `skip_reason="in_scope_defer_to_hitl"`) → the inbound endpoint acks the customer + creates a HITL ticket.
2. A factual/off-topic ask ("Какое сегодня число?", "Который час?", "Расскажи анекдот") still gets a scope **decline** — no operator noise.
3. An **out-of-scope** booking ("Где забронировать отель?", Story 12.34) still **declines**, not escalates.
4. A **disabled / noop project** (default — no calendar) keeps declining a booking ask (no behaviour change): the defer fires only when `project_does_bookings` is true (wired to calendar `is_enabled`).
5. **(Round-6 D10 #34)** A booking expressed by **structure** rather than a scheduling verb — "Можно багги сегодня в 13:00, нас четверо" — also defers: it parses to a concrete start via `extract_requested_start` even though the verb-based `has_scheduling_intent` misses it. A factual question with no concrete time ("Какое сегодня число?") still parses to `None` → declines (no false positive).
6. Story 12.09 (persona thin gate) unchanged; gates green; 100% coverage.

## Design — two guards

The defer fires only when **both** hold:
1. **`project_does_bookings(project_id)`** — the project offers booking/sales work (live: calendar `is_enabled`). A disabled noop project keeps the plain decline, so off-topic never becomes a ticket. This is what keeps the `test_e2e_epic11_disabled_noop` decline test green.
2. **Precise in-scope intent** — `has_scheduling_intent(normalized)` OR `classify_turn().kind ∈ {price_ask, catalog_ask}` OR **parses as a concrete booking** (`extract_requested_start is not None`, round-6 D10 #34), AND NOT `is_out_of_scope`.

Deferring (skip) — not answering — is the mechanism: `AnswerPipeline.run` returns `handled=False` when nothing handles, and the inbound endpoint then escalates to HITL. Same skip→HITL pattern as D11 (#118).

## Out of scope (documented)

- A project that sells priced services but has **calendar disabled** would, with this proxy, still decline a price ask (current behaviour — no regression). Refining `project_does_bookings` to a dedicated "sales-enabled" flag is a follow-up if that configuration appears; calendar `is_enabled` is the available proxy for "this project does bookings", and in practice the sales-persona projects have calendar enabled.

## Tasks / Subtasks

- [x] `ScopeGuardAnswerer`: precise `_is_in_scope` (scheduling/price/catalog, minus out-of-scope) + `project_does_bookings` gate; defer (`handled=False`) when both hold, else decline.
- [x] Wire `project_does_bookings=calendar_settings_repository.is_enabled` (and the shared normalizer) in the live pipeline (`main.py`).
- [x] Tests (TDD): defer for price + booking asks (bookings on); decline for factual, out-of-scope, and the no-bookings project; inbound integration → escalates (ack + ticket).

## Dev Notes

- **Skip → HITL mechanism verified:** `AnswerPipeline.run` returns `AnswerResult(handled=False)` when no answerer handles; the inbound `handled is False` branch acks + creates a ticket + DMs the operator.
- **Cost:** the intent signals run only for messages that already fell through to the last resort (the off-topic minority); the DB `is_enabled` lookup runs only when the ask is in-scope.
- **Files:** `services/api/app/answerers/scope_guard.py`, `services/api/app/main.py`.

## References

- Round-5 QA Finding D10 (in-scope ask declined "Этим не занимаюсь.").
- Precise-signal probe (this story): `is_sales_intent` false-positives vs `has_scheduling_intent`/`classify_turn`.
- Contract preserved: Story 12.09 "thin gate"; the `test_e2e_epic11_disabled_noop` decline contract.
- [Source: services/api/app/answerers/scope_guard.py#try_answer], [services/api/app/main.py#answer_pipeline].

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–5, D10 ScopeGuard).** ScopeGuard defers a precise in-scope ask to HITL on bookings-enabled projects; declines factual/out-of-scope/disabled-project asks unchanged.
- **Naive fix rejected with evidence:** gating on `is_sales_intent` breaks the datetime decline e2e test (false positive). Replaced by the precise signal + project-config guard; probe table in this doc.
- **Zero regression:** the disabled-noop decline test stays green via the `project_does_bookings` guard.
- **TDD:** all defer/decline branches watched RED first; full suite green at 100% coverage; `scope_guard.py` 100%.

### File List

- `services/api/app/answerers/scope_guard.py` (modified)
- `services/api/app/main.py` (modified — pipeline wiring)
- `tests/test_scope_guard_answerer.py` (modified — defer/decline/escalation tests)
- `_bmad-output/implementation-artifacts/12-39-scope-guard-defers-inscope.md` (new)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified)
