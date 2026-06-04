# Story 12.63: Derive a buggy-count recommendation from the catalog, else HITL (round-15)

Status: review

## Story
As a customer asking «сколько багги на 8 человек?», I want a buggy-count recommendation when the catalog states per-buggy capacity; otherwise an honest escalation.

## Context
Per the round-15 decision ("derive from catalog/RAG, if not possible then HITL"). Verified against the live RAG: it has a *quadbike* team-event figure («2 чел. на квадрике») and buggy **models** (Yamaha Viking 700 / BRP Sport Trail 1000) but **no stated per-buggy capacity** — so today this groundwork escalates; it auto-derives once a «Багги — до N человек» line exists.

## Fix
`_handle_capacity_question` now derives first: `_parse_headcount` (digits or a Russian numeral/collective like «восемь»/«двое») + `_buggy_seats_from_catalog` (RAG scan; `_parse_buggy_seats` is **buggy-specific** — requires «багг» beside the number, so a quadbike figure never leaks) → `ceil(headcount/seats)` buggies. Reply «На N человек понадобится примерно M багги.» (no escalation). When headcount or seats can't be obtained → the existing HITL escalation («Уточняю у коллег…»).

## Acceptance Criteria
1. Catalog states buggy capacity → a deterministic buggy-count recommendation; no quadbike figure ever used. ✅
2. No derivable data → HITL escalation (current behavior). ✅ (live today)
3. Headcount parses digits and common Russian numerals/collectives. ✅
4. Gates green; 100% coverage. ✅

## Files
- `services/api/app/sales/sales_persona_answerer.py` (`_parse_headcount`, `_parse_buggy_seats`, `_buggy_seats_from_catalog`, `_derive_buggy_count`, async `_handle_capacity_question`)
- `tests/test_sales_persona_answerer_early_busy_check.py`
