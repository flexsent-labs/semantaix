---
title: 'Humanize and normalize RAG catalog fallback output'
type: 'bugfix'
created: '2026-08-06'
status: 'draft'
---

## Intent

**Problem:** When the LLM answer is unavailable, a broad services question falls
back to raw RAG excerpts. The response exposes internal wording, OCR artifacts,
irrelevant punctuation, and all-uppercase source text, so it sounds like a data
dump instead of a helpful service representative.

**Approach:** Keep the existing bounded, deduplicated, non-confidential fallback,
but normalize each displayed item before rendering it and use a natural Russian
introduction and closing. The response must present available options directly;
it must not mention materials, documents, RAG, search, or retrieval.

## Boundaries & Constraints

**Always:** preserve the current confidentiality filter, duplicate removal, item
limit, and truncation; normalize Unicode, whitespace, bullets, OCR symbols, and
leading punctuation; convert source lines that are entirely uppercase to normal
sentence case; keep the result in Russian where the source is Russian.

**Ask First:** none.

**Never:** invent services, prices, availability, or other facts; expose
confidential chunks; change retrieval ranking, intent classification, or LLM
prompts as part of this fix.

## I/O & Edge-Case Matrix

| Input | Expected result |
|---|---|
| Broad catalog query with mixed source excerpts | Human-readable bounded list with natural copy |
| All-uppercase source line | Sentence case, without an all-uppercase display line |
| OCR marks such as `©`, `®`, stray quotes, or leading commas | Marks removed and whitespace/punctuation normalized |
| Confidential, duplicate, empty, or junk-only chunk | Omitted as before |
| No usable public items | Return `None` so the caller can continue its existing path |

## Code Map

- `services/api/app/answerers/grounded_rag.py`: normalize catalog items and
  render humanized fallback copy.
- `tests/test_answerers_grounded_rag.py`: regression tests for casing, OCR
  cleanup, internal wording, deduplication, confidentiality, and bounds.

## Tasks

1. Add a private catalog-item normalization helper with conservative cleanup.
2. Apply it before deduplication, truncation, and item rendering.
3. Replace internal/source-oriented fallback wording with direct customer copy.
4. Add regression coverage for the reported uppercase/OCR example and retain
   the existing fallback safeguards.

## Acceptance Criteria

- The fallback never contains `По материалам компании` or equivalent internal
  retrieval wording.
- The reported output contains no `©`, `®`, stray leading comma, or all-uppercase
  item line after normalization.
- Duplicate, confidential, empty, and over-limit behavior remains unchanged.
- No factual content is generated beyond the normalized source excerpts.
- Focused tests and lint pass; the full test suite remains green.

## Verification

- `ruff check services/api/app/answerers/grounded_rag.py tests/test_answerers_grounded_rag.py`
- `pytest tests/test_answerers_grounded_rag.py -q`
- `pytest --cov --cov-config=.coveragerc --cov-report=term-missing`
