# Story 12.34: Decline out-of-scope requests instead of "передам коллегам" (D7, P2)

Status: review

## Story

As a **customer who asks for something the buggy business doesn't offer**,
I want the bot to **politely say it's out of scope and steer back to buggy bookings**,
so that **a restaurant or hotel request isn't accepted as a booking ("передам коллегам на подтверждение").**

**Problem (observed live, багги, 1 June 2026 10:53):**

```
Artur:  А посоветуйте, пожалуйста, хороший ресторан рядом с водопадом и какую машину взять в аренду?
Анна:   Спасибо! Передам детали коллегам на подтверждение — вернутся с ответом.
```

**Root cause (CONFIRMED — see investigation Finding D7):** the polite-decline `ScopeGuardAnswerer` exists but runs **last** in the pipeline (`main.py:646-681`); it only fires if every earlier answerer skips. An out-of-scope phrase is `is_sales_intent=False` / `classify_turn="other"` (verified), so in a **clean** state it skips to downstream — but once a sales conversation is **active**, `_handle_scoping`/`_handle_pitching` claim the turn as a booking answer and emit `SCOPING_COMPLETE_HANDOFF_LINE`. `_dispatch` has no out-of-scope branch (only Story 12.27's `is_cancellation`).

## Acceptance Criteria

1. An unambiguous out-of-scope request (dining/lodging venue: ресторан, кафе, отель, гостиница, …) → a short polite decline that redirects to buggy bookings; NEVER the booking-acceptance line. Fires in any stage (clean or mid-funnel).
2. A genuine buggy request is never declined: the branch only fires when the turn is **not** a sales intent, so a mixed "хочу багги … и ресторан?" stays in the funnel, and a field answer ("двое") is untouched.
3. Mid-funnel, the decline does **not** mutate funnel state — the booking is parked and the customer resumes on the next on-topic turn.
4. In-scope booking flow unchanged; gates green; 100% coverage.

## Tasks / Subtasks

- [x] `services/api/app/sales/out_of_scope.py`: `is_out_of_scope(text, normalizer)` — a conservative lemma denylist of dining/lodging venue nouns (never present in a buggy-booking field answer), sibling of `cancel_intent.py`/`decline.py`.
- [x] `OUT_OF_SCOPE_DECLINE_LINE` + `_handle_out_of_scope` (handled reply, `suppress_followup`, no escalation, no state mutation).
- [x] `_dispatch` branch after `is_cancellation`: `if is_out_of_scope(q) and not is_sales_intent(q): decline`.
- [x] Tests: detector positives (ресторан/кафе/отель/гостиница) + negatives (field answers, buggy intent, cancellation); dispatch in clean state, mid-scoping (state preserved), mixed-with-sales-intent not declined.

## Dev Notes

- **Conservative by design.** The denylist targets only unambiguous separate-service venues (dining/lodging) that never appear in a buggy booking, plus the `and not is_sales_intent` guard — so it cannot wrongly decline a real booking turn (the cost of a false decline is worse than the current over-accept). It catches the observed case (ресторан); a fully-general scope classifier (car-rental, weather, arbitrary chit-chat) is a larger follow-up — the denylist is extensible.
- **Why a persona-side decline, not "skip to ScopeGuardAnswerer":** a skip in an active funnel doesn't reliably reach the last-in-pipeline ScopeGuard (the GroundedRag answerer runs first and may escalate). A direct decline in `_dispatch` fires in any stage and keeps the funnel parked.
- **Files:** `services/api/app/sales/out_of_scope.py` (new), `services/api/app/sales/sales_persona_answerer.py`.
- **Reuse:** the Story 12.27 `is_cancellation` early-dispatch pattern; `RussianNormalizer.lemmas`.

## References

- Investigation: round-3/4 validation, Finding D7 (ScopeGuard is last-only; funnel claims active-state off-topic turns).
- [Source: services/api/app/sales/sales_persona_answerer.py#_dispatch], [#_handle_out_of_scope].
- Precedent: `12-27-cancellation-intent-route-to-human.md` (`cancel_intent.py` + early dispatch branch); `spec-scope-guard-polite-decline.md`.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–4).** New `out_of_scope.is_out_of_scope` (conservative dining/lodging lemma denylist) + `OUT_OF_SCOPE_DECLINE_LINE` + `_handle_out_of_scope`; `_dispatch` branch after `is_cancellation`, gated `and not is_sales_intent`. Fires in any stage, no escalation, no `_persist` (funnel parked), `suppress_followup`.
- **Empirically confirmed** the trigger: "посоветуйте ресторан…" → `is_sales_intent=False`/`turn=other`, so in a clean state it skipped downstream; the live booking-acceptance line came from an active funnel claiming the turn. The new branch intercepts it in any stage.
- **TDD:** tests written first and watched fail (`ModuleNotFoundError: out_of_scope`), then green — detector positives/negatives, clean-state decline, mid-scoping decline (state preserved), mixed-with-sales-intent NOT declined.
- `ruff` clean; full suite **3120 passed at 100% coverage**; `out_of_scope.py` + `sales_persona_answerer.py` 100%.

### File List

- `services/api/app/sales/out_of_scope.py` (new)
- `services/api/app/sales/sales_persona_answerer.py` (modified)
- `tests/test_sales_persona_answerer_out_of_scope.py` (new)
- `_bmad-output/implementation-artifacts/12-34-decline-out-of-scope.md` (new)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified)
