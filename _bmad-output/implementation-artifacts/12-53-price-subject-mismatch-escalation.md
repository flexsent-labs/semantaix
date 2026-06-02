# Story 12.53: A price ask never quotes an off-subject price (round-11 N2, P2)

Status: review

## Story

As a **customer asking the price of «багги»**, I want **a buggy price or an honest "I'll check"**, so that **I'm never quoted a «квадроцикл» price for a buggy ask** — even though no services are configured.

**Problem (live, round 11):** «нас 12 человек. Сколько багги нужно и сколько это будет стоить?» → «Стоимость от 30 000 ₽ за квадроцикл». A buggy ask returns a quadbike price.

## Root cause (CONFIRMED)

Two live facts (verified against the container DBs): (1) the project has **0 configured services** (`semantaix_sales.db.services` is empty), and (2) the RAG holds **only quadbike/enduro/scooter prices** — every price chunk says «ЗА КВАДРОЦИКЛ»; there is no buggy price. So in `price_lookup.lookup`, `_best_matching_service(question, service_names=(), …)` returns `None` (nothing to match against), the 12.49 per-service guard is **skipped**, and the first price-bearing chunk — a quadbike price — wins. 12.49's tests passed because they fabricated `service_names=["Аренда багги","Квадроцикл"]`; live has none.

## Fix (per the chosen direction: escalate, never mis-quote)

When no service resolves (`asked is None`), apply a **catalog-free subject check**: the winning price chunk must share a *distinctive subject lemma* with the question, else skip it (→ `PriceMissing` → the bot escalates «уточню у коллег» instead of quoting). `_subject_lemmas` = the question's lemmas minus a tunable generic set (`_PRICE_GENERIC_LEMMAS`: price words, booking/activity-generic, units, function words) and bare numbers — leaving the distinctive noun («багга», «квадроцикл», «каньонинг»). A generic ask («сколько стоит?») has no subject → first-price-chunk behaviour is preserved.

This works with zero services configured (the live case): «… багги?» → subject `{багга}`; a «квадроцикл» chunk lacks it → escalate. When services ARE configured, the stronger 12.49 per-service guard runs first and this is a no-op.

## Acceptance Criteria

1. A «багги» ask never returns a «квадроцикл» price, even with empty `service_names`; it escalates (`PriceMissing`). ✅
2. A generic ask (no subject named) is unchanged (first price chunk). ✅
3. A subject-matching chunk («Багги — 90 000 ₽…») is still quoted. ✅
4. Deterministic; gates green; 100% coverage. ✅

## Note: data/config remains required

The bot is a "buggy rental" persona over a quadbike tour catalog with no buggy pricing and no services. This story makes the bot **honest** (escalate, never mis-quote) but cannot produce a buggy price that doesn't exist; quoting a real buggy price needs the operator to configure the buggy service(s) + add buggy pricing to the RAG. The dropped capacity-recommendation («сколько багги для 12 человек») is a separate feature gap, not addressed here.

## Tasks / Subtasks

- [x] `_PRICE_GENERIC_LEMMAS` + `_subject_lemmas`; add the `asked is None and subject and no-overlap → skip` branch to `lookup`.
- [x] Tests (TDD): no-services buggy ask → `PriceMissing`; no-services generic ask → `PriceFound`; no-services subject-match → `PriceFound`.

## Dev Notes

- The generic-lemma set is tunable (like the guardrail lists). The failure modes are asymmetric by design: a *missing* generic word risks only a (safe) escalation; a distinctive subject noun must never be added to the set (would re-enable a wrong quote).
- Numbers are excluded from the subject so "6 часов" / quantities don't create spurious overlap with a chunk.
- **Files:** `services/api/app/sales/price_lookup.py`.

## References

- Round-11 live QA Defect N2 (regression of 12.49 against live data). Investigation: `investigations/booking-round11-investigation.md`.
- [Source: price_lookup.py#_subject_lemmas], [#PriceLookup.lookup].

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–4).** With no services configured, a subject-naming price ask must hit a subject-matching chunk or escalate, so a buggy ask never returns a quadbike price. The deeper data/config gap (no buggy service, no buggy price) is documented as an operator task. TDD; full suite green at 100% coverage.

### File List

- `services/api/app/sales/price_lookup.py` (modified — `_PRICE_GENERIC_LEMMAS` + `_subject_lemmas` + subject guard)
- `tests/test_sales_price_lookup.py` (modified — no-services subject-mismatch / generic / match tests)
- `_bmad-output/implementation-artifacts/12-53-price-subject-mismatch-escalation.md` (new)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified)
