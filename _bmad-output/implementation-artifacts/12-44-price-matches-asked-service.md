# Story 12.44: Price answer matches the asked service (round-8 N2, P2)

Status: review

## Story

As a **customer asking the price of a багги**, I want **the багги price**, so that **I'm not quoted the quadbike rate.**

**Problem (live, round 8):**
```
Сколько стоит покататься на багги?   → Стоимость составляет 13 000 ₽ за квадроцикл.   ❌
```
The price intent fired correctly (good — D10/D4 path works), but the quoted line was for **квадроцикл**, a different service.

## Root cause (CONFIRMED)

`PriceLookup.lookup` is RAG-retrieval-based: it returns the **first retrieved chunk that carries a price token**, with **no check that the chunk names the asked service**. For «… багги?», the highest-scoring price-bearing chunk happened to be the квадроцикл row, and the LLM faithfully quoted it.

## Fix

`PriceLookup.lookup` is now **service-aware** (new `service_names` arg, the project's configured catalog):
- `_asked_service(question, service_names)` — the configured service the question names (lemma-subset match; «… багги?» → "Багги"). `None` for a generic ask.
- When an asked service is identified, a winning chunk must also **mention that service** (`_chunk_mentions_service`, lemma match); chunks for other services are skipped. If no price chunk matches → `PriceMissing` (carrying the asked service), which the persona renders as "no fixed price — ask route/headcount" / escalates (AC3) — never a wrong-service quote.
- A generic ask that names no configured service keeps the first-price-chunk behaviour (unchanged).

The persona's `_handle_pricing` fetches the catalog (`services_repo.list_for_project`) and passes the names to the lookup.

## Acceptance Criteria

1. «… багги?» → a багги price, or "no fixed price, here's how it's priced" — the named service in the reply matches the ask; **never квадроцикл**. ✅
2. «… квадроцикл?» → the quadbike price (mapping correct both ways). ✅
3. Asked service has no fixed-price chunk → `PriceMissing` (ask route/headcount), not another service's price. ✅
4. Gates green; 100% coverage. ✅

## Tasks / Subtasks

- [x] `_asked_service` + `_chunk_mentions_service` (lemma-space) in `price_lookup.py`; `lookup(service_names=...)` filters by asked service; `PriceMissing` carries the asked service.
- [x] `_handle_pricing` passes the configured catalog names to the lookup.
- [x] Tests (TDD): lookup skips a non-asked-service price chunk; returns the matching-service chunk; quadbike↔buggy both ways; generic ask unchanged; persona-wiring test asserts the catalog flows through (+ blank names filtered).
- [x] Updated the 5 fake price-lookups in the pricing tests to accept the new kwarg.

## Dev Notes

- **Lemma space:** pymorphy3 lemmatizes "Багги" → `багга`; both `_asked_service` and `_chunk_mentions_service` run through the normalizer, so matching is inflection-robust and consistent.
- **Conservative:** a generic «сколько это стоит?» (no service named) is unchanged — the filter only engages when the question names a configured service.
- **Data note:** if the KB genuinely has no багги price, this correctly returns `PriceMissing` (→ ask route/headcount) instead of quoting квадроцикл — surfacing the gap honestly rather than misquoting.
- **Files:** `services/api/app/sales/price_lookup.py`, `services/api/app/sales/sales_persona_answerer.py`.

## References

- Round-8 live QA Defect N2.
- [Source: price_lookup.py#PriceLookup.lookup], [#_asked_service], [#_chunk_mentions_service]; [sales_persona_answerer.py#_handle_pricing].
- Related: D4 (answer price before pushing for a date).

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–4).** The price lookup now requires the quoted chunk to name the asked service; a багги question can no longer be answered with the квадроцикл rate, and a missing багги price returns `PriceMissing` (ask route/headcount) rather than a wrong-service quote.
- **TDD:** lookup-level service-match tests + a persona wiring test (catalog flows through, blanks filtered). Full suite green at 100% coverage.

### File List

- `services/api/app/sales/price_lookup.py` (modified — service-match)
- `services/api/app/sales/sales_persona_answerer.py` (modified — `_handle_pricing` passes the catalog)
- `tests/test_sales_price_lookup.py` (modified — 4 service-match tests)
- `tests/test_sales_persona_answerer_pricing_hit.py` (modified — wiring test + stub/build)
- `tests/test_sales_persona_answerer_pricing_{miss,quote_drift,rag_unavailable}.py`, `tests/test_sales_persona_answerer_first_turn_price.py` (modified — fake lookup accepts the new kwarg)
- `_bmad-output/implementation-artifacts/12-44-price-matches-asked-service.md` (new)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified)
