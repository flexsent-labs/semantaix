# Story 12.46: A price ask must not wedge the conversation (round-9 R9-1, P1)

Status: review

## Story

As a **customer who asks the price then continues (greeting, booking)**, I want **each later message handled on its own merits**, so that **the bot doesn't answer my greeting or my availability question with a price.**

**Problem (live, round 9, 2 Jun 2026):** after a price ask, *every* later turn got a price reply — a greeting → «…13 000 ₽ за квадроцикл», a booking → a price line, an availability question → «Уточню у коллег…». The booking happy-path was unreachable.

## Root cause (CONFIRMED)

The pricing stages are **sticky**. In `_dispatch`:
```python
if current_stage in (STAGE_PRICING, STAGE_AWAITING_OPERATOR_PRICE):
    return await self._handle_pricing(question=question, ctx=ctx, state=state)
```
Once a price ask put the funnel in `pricing` / `awaiting_operator_price`, **every** subsequent message — greeting, booking, availability — was routed straight back into `_handle_pricing`, which ran a RAG price lookup on that text and returned a price line. A greeting like «Здравствуйте!» → price lookup → first price chunk → a квадроцикл quote. This is the umbrella for the round-9 "N2 квадроцикл on a greeting" and much of the "N3 уточню" noise.

## Fix

A price ask is a **one-shot aside**, not a trap. In the pricing branch, only re-run pricing when the customer is *still* on pricing; when they've **moved on** (a fresh greeting, or a parseable booking/availability turn), re-enter the funnel from greeting so the turn is handled on its own merits. `_has_moved_on_from_pricing(question, ctx)` returns True for a leading greeting (`_GREETING_RE`) or a turn that parses to a concrete booking (`extract_requested_start`); a price follow-up («ну так сколько в итоге?», «сколько?») returns False and **stays** in pricing. (Cancellation / out-of-scope are already routed earlier in `_dispatch`.)

## Acceptance Criteria

1. A price ask is answered on its own turn; it does not consume or attach to a later, unrelated message. ✅ (the "later price" in round 9 was the sticky stage answering a greeting — eliminated.)
2. After a price ask, the next greeting → a greeting reply; the next availability question → a verdict — never a price line. ✅
3. No sticky state: each inbound is re-evaluated (price → pricing, booking → availability, greeting → greeting). ✅
4. Repro `price → greeting → availability(busy)`: greeting reply, then «занято»+alt — each on its own turn. ✅
5. A genuine price follow-up still re-enters pricing (no regression to the awaiting-operator-price flow). ✅
6. Gates green; 100% coverage.

## Round-9 disposition (the other items)

- **N1 (dotted date)** — **already fixed + deployed** (12.43). Verified live in the container: `extract_requested_start("Можно 03.06 в 16:00…") → 2026-06-03 16:00`. Round 9's dotted failures (19:44/19:46) predate the 12.43 deploy → a clean re-test confirms.
- **N2 (wrong-service price)** — the round-9 «квадроцикл» appeared on a **greeting** routed to the price lookup by *this* sticky-stage bug; 12.44's service-match is correct for real price asks, and this fix removes the greeting→price path. No additional N2 code change.
- **N3 (EN → RU)** — 12.45 mirrors the **LLM** lines (greeting/scoping/pricing). The round-9 EN→«Уточню у коллег» is the **deterministic price-defer constant** (RU-only) — the bilingual-constants follow-up (12.47), not a 12.45 failure.

## Tasks / Subtasks

- [x] `_has_moved_on_from_pricing` helper (`_GREETING_RE` greeting OR `extract_requested_start` booking) + re-route in the `_dispatch` pricing branch.
- [x] Tests (TDD): greeting-after-pricing → greeting (not price); booking-after-pricing → «занято» (not price). The existing price-follow-up tests («сколько?», «ну так сколько в итоге?») still stay in pricing.

## Dev Notes

- **Default = stay in pricing**, re-route only on a positive "moved-on" signal — so elliptical price follow-ups (which `classify_turn` does NOT tag `price_ask`) are not wrongly ejected. (My first attempt gated on `classify_turn == price_ask` and broke those two tests; the positive-signal gate is correct.)
- **Files:** `services/api/app/sales/sales_persona_answerer.py`.

## References

- Round-9 live QA Defect R9-1. Related: Round-7 stuck-state note.
- [Source: sales_persona_answerer.py#_dispatch], [#_has_moved_on_from_pricing].

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–6).** The pricing stage no longer traps the customer; a greeting/booking after a price ask re-enters the funnel, a price follow-up stays. Resolves the round-9 wedge + the N2-on-greeting symptom.
- **N1 already fixed/deployed; N2 residual was this bug; N3 LLM-mirroring works (constants → 12.47).** TDD; full suite green at 100% coverage.

### File List

- `services/api/app/sales/sales_persona_answerer.py` (modified — `_GREETING_RE` + `_has_moved_on_from_pricing` + dispatch re-route)
- `tests/test_sales_persona_answerer_early_busy_check.py` (modified — R9-1 greeting/booking re-route tests)
- `_bmad-output/implementation-artifacts/12-46-pricing-aside-not-sticky.md` (new)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified)
