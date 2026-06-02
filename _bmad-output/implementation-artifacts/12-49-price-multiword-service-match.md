# Story 12.49: A price ask resolves the right multi-word service (round-10 N2, P2)

Status: review

## Story

As a **customer asking the price of a service whose configured name is multi-word («Аренда багги»)**, I want **the price of THAT service**, so that **«Сколько стоит покататься на багги?» never returns a квадроцикл price.**

**Problem (live, round 10, 2 Jun 2026, 20:46):** «Сколько стоит покататься на багги?» → «Стоимость составляет **13 000 ₽ за квадроцикл**». A багги ask returned a квадроцикл price, immediately and repeatably.

## Root cause (CONFIRMED)

The 12.44 service-match used a **lemma-subset** test: a chunk/question "mentioned" a service only if the service-name's lemmas were a **subset** of the text's lemmas. For the single-word seed «Багги» that worked, but the project's real service is the **multi-word** «Аренда багги» (lemmas `{аренда, багга}`). The customer's «… покататься на багги?» contains `багга` but not `аренда`, so the subset test failed → the question resolved to **no** service → the guard was bypassed → the first price-bearing chunk (a квадроцикл line) was quoted. Subset matching is structurally wrong for multi-word service names.

## Fix

Replace subset matching with **max lemma-overlap**. `_best_matching_service(text, service_names, normalizer)` returns the configured service whose name shares the **most** lemmas with `text` (ties/zero-overlap → `None`). Run the SAME routine on the question and on each candidate chunk:

- The question «… на багги?» overlaps «Аренда багги» on `багга` (overlap 1) and «Квадроцикл» on nothing → resolves to «Аренда багги».
- A price chunk is kept only when ITS best-matching service equals the asked one; a квадроцикл chunk resolves to «Квадроцикл» ≠ «Аренда багги» → skipped.

Running the same symmetric routine both sides makes shared generic words ("аренда"/"прокат") harmless — the distinctive noun («багга» vs «квадроцикл») decides the winner. A generic ask that names no service (overlap 0 → `None`) keeps the first-price-chunk behaviour. `PriceMissing` now carries `service=asked` so the operator ticket names the resolved service.

## Acceptance Criteria

1. Every price reply names the asked service; a багги ask never returns a квадроцикл price — even when the configured name is multi-word «Аренда багги». ✅
2. Buggy and quadbike asks map to their own services, both directions. ✅
3. A generic price ask (no service named) is unaffected (first-price-chunk). ✅
4. Deterministic — same ask, same answer. Gates green; 100% coverage. ✅

## Tasks / Subtasks

- [x] Replace `_asked_service`/`_chunk_mentions_service` (subset) with `_best_matching_service` (max-overlap); rewire `lookup`.
- [x] Tests (TDD): `_MULTIWORD_SERVICES = ["Аренда багги", "Квадроцикл"]` — short «багги» word resolves to «Аренда багги» and returns its price; a no-overlap ask misses (PriceMissing) rather than returning the wrong service.

## Dev Notes

- Symmetric max-overlap (question side AND chunk side) is the key: a one-sided "name ⊆ text" test cannot handle a multi-word name when the customer abbreviates. Overlap of the distinctive lemma is enough; generic shared lemmas don't tip the result because BOTH services would share them equally.
- 12.44's earlier subset refactor also produced a coverage miss (a dead `return False` guard); the single max-overlap routine removes that branch entirely.
- **Files:** `services/api/app/sales/price_lookup.py`.

## References

- Round-10 live QA Defect N2. Supersedes the subset match introduced in Story 12.44.
- [Source: price_lookup.py#_best_matching_service], [#PriceLookup.lookup].

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–4).** Max lemma-overlap resolves the configured multi-word service from the customer's short word, both on the question and on each chunk, so a багги ask returns a багги price and a квадроцикл chunk is skipped. TDD; 17 price-lookup tests + full suite green at 100% coverage.

### File List

- `services/api/app/sales/price_lookup.py` (modified — `_best_matching_service` replaces subset match; `lookup` rewired)
- `tests/test_sales_price_lookup.py` (modified — multi-word resolve + no-match-misses tests)
- `_bmad-output/implementation-artifacts/12-49-price-multiword-service-match.md` (new)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified)
