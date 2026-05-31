# Story 12.28: Answer a price question on the first turn instead of asking for a date

Status: review

## Story

As a **customer who opens with a price question** ("8 человек, сколько стоит?"),
I want the bot to **answer the price (or escalate if it doesn't know)**,
so that **I get the information I asked for instead of being pushed into a date-collection funnel that ignores my question and my group size**.

**Problem (observed live, багги, 31 May 2026, "Анна Иванова"):**

```
Customer: 8 человек, сколько стоит?
Анна:     На какую дату планируете?     ← price + group size both dropped
```

**Root cause** (`services/api/app/sales/sales_persona_answerer.py`): the per-turn classifier `classify_turn` *correctly* tags "8 человек, сколько стоит?" as `price_ask`, but it is consulted ONLY inside `_maybe_handle_aside`, which runs ONLY when `current_stage ∈ {scoping, pitching}` (`_ASIDE_INTERCEPT_STAGES`). On first contact `state is None`, so `_dispatch` routes straight to `_handle_greeting`, which never classifies the turn and never calls `price_lookup`. The greeting LLM extracts `headcount=8`, the greeting prompt forbids quoting prices, the stage transitions to `scoping`, and the next-field logic asks the top-missing field `dates`. The price question is silently dropped.

## Acceptance Criteria

1. **First-turn price hit → quote.** `"8 человек, сколько стоит?"` with a `PriceFound` → the bot's first reply contains the verbatim price token, `stage_after=STAGE_PRICING`, `sales_turn_kind="pricing_hit"`, and is NOT the date question.
2. **Group size captured.** The `headcount` extracted from the opener reaches `price_lookup` (`intent.headcount == 8`) and is persisted in the `pricing` state, so the funnel never re-asks "сколько человек?".
3. **First-turn price miss → escalate.** A `PriceMissing` → `RESPONSE_MODE_SALES_ESCALATION`, `escalate=True`, `hitl_reason='price_unknown'`, `stage_after=STAGE_AWAITING_OPERATOR_PRICE` — still not a date question. The miss path skips the pricing LLM (only the greeting call fires).
4. **Non-price opener unaffected.** `"хочу багги завтра"` → original greeting flow: `stage_after=STAGE_SCOPING`, LLM next-field question delivered, `price_lookup` never called.
5. **Pricing not configured → fall through.** With no `price_lookup` wired, a first-turn price ask proceeds through the normal greeting → scoping flow (guard, no crash).
6. **Gates green.** `ruff check .` clean; full suite at 100% coverage on `platform_common/` + `services/`.

## Tasks / Subtasks

- [x] **Helper `_maybe_intercept_price_ask`** (AC 1–5) — new private async method: returns `None` when `self._price_lookup is None` or `classify_turn(question).kind != "price_ask"`; otherwise routes to the existing `_handle_pricing` with a synthetic state `{current_stage: STAGE_NEW, collected_intent: merged.to_dict()}` so the just-extracted fields inform the quote and are persisted.
- [x] **Hook into `_handle_greeting`** (AC 1–4) — call the helper after the Story 12.25 busy-slot intercept and before the `STAGE_SCOPING` transition; return its `AnswerResult` when non-`None`. Busy-slot intercept keeps precedence (a concrete busy time still wins).
- [x] **TDD test file** (AC 1–5) — new `tests/test_sales_persona_answerer_first_turn_price.py`: price hit, group-size capture, price miss escalation, non-price opener regression, pricing-not-configured guard.
- [x] **Gates** (AC 6) — `ruff` clean; 587 sales tests green.

## Dev Notes

- **Files:** `services/api/app/sales/sales_persona_answerer.py` (new `_maybe_intercept_price_ask` + greeting hook); `tests/test_sales_persona_answerer_first_turn_price.py` (new).
- **Reuse, don't reinvent:** routes to the existing `_handle_pricing` (Story 12.04) unchanged — same KB-first quote / escalate-if-unknown / quote-drift guard. `classify_turn` (Story 12.06) is the same price detector used mid-funnel; this story just makes it reachable on turn 1. The synthetic state mirrors the dict shape `_handle_pricing` already reads (`state.get("current_stage")`, `state.get("collected_intent")`), so no change to the pricing handler.
- **Why intercept inside `_handle_greeting` (after the LLM), not in `_dispatch` before it:** running the greeting extraction first captures `headcount` from the opener so it is persisted and not re-asked — addressing the "group size ignored" half of the complaint. The extra LLM call on a first-contact price ask is the cost; the alternative (intercept before greeting) would drop the headcount.
- **Why busy-slot intercept keeps precedence:** an opener carrying both a concrete busy time and a price ask ("завтра в 14:00, сколько стоит?") should hear that the slot won't work before a price; if the time is free, the busy intercept returns `None` and the price intercept fires.
- **Known accepted limitation (out of scope):** `price_lookup._build_query` ignores `intent`/`headcount` (`price_lookup.py:99-110`), so the quoted price is still whatever RAG returns — not a group-of-8 tier. Capturing `headcount` here fixes "group size dropped/re-asked"; group-size-*aware* pricing is a separate, larger change (noted in the investigation's Side Findings).
- **Conventions:** dispatch never raises; immutable `Intent`; `from __future__ import annotations`; ruff E/F/I line-100; 100% coverage gate.

### References

- Story 12.04 (pricing turn) / 12.06 (asides + `classify_turn`) / 12.25 (greeting early-intercept pattern this mirrors).
- Investigation: `_bmad-output/implementation-artifacts/investigations/booking-dialog-bugs-investigation.md` (Finding 2).
