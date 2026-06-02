"""Unit tests for `PriceLookup` (Story 12.04).

Covers:
  * Query construction includes the customer's question + price anchors.
  * The digit-then-currency regex matches every currency form the story
    enumerates (₽, руб, р., RUB) — including thin/non-breaking spaces.
  * A non-price chunk is excluded even when its other lemmas overlap.
  * The first chunk carrying a price token wins (even when a higher-
    scoring chunk is returned but has no price).
  * On a miss, ``PriceMissing.payload.original_question`` carries the
    customer's verbatim text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from services.api.app.rag import RagChunk
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.intent import Intent
from services.api.app.sales.price_lookup import (
    PriceFound,
    PriceLookup,
    PriceMissing,
    PriceUnknownPayload,
    extract_price_tokens,
)


@dataclass
class _FakeRagRetriever:
    chunks: list[RagChunk]
    calls: list[dict[str, Any]]

    def retrieve(
        self,
        *,
        query: str,
        limit: int = 3,
        project_id: int | None = None,
    ) -> list[RagChunk]:
        self.calls.append(
            {"query": query, "limit": limit, "project_id": project_id}
        )
        return list(self.chunks)


def _retriever(chunks: list[RagChunk]) -> _FakeRagRetriever:
    return _FakeRagRetriever(chunks=list(chunks), calls=[])


def _lookup(chunks: list[RagChunk]) -> tuple[PriceLookup, _FakeRagRetriever]:
    retriever = _retriever(chunks)
    return (
        PriceLookup(
            rag_retriever=retriever, normalizer=get_russian_normalizer()
        ),
        retriever,
    )


@pytest.mark.parametrize(
    "snippet",
    [
        "Цена тура 15 000 ₽ за группу",
        "Стоимость 15000 руб с человека",
        "Прокат 5 000 р. за час",
        "Итого 15000 RUB",
    ],
)
def test_price_regex_matches_all_currency_forms(snippet: str) -> None:
    tokens = extract_price_tokens(snippet)
    assert tokens, snippet


def test_price_regex_handles_non_breaking_space_in_thousands() -> None:
    # The RU thousands separator is often a non-breaking space (U+00A0).
    snippet = "Цена 15 000 ₽ за группу."
    assert extract_price_tokens(snippet)


def test_non_price_chunk_is_not_matched() -> None:
    assert not extract_price_tokens("Тур длится 6 часов и включает обед.")


@pytest.mark.asyncio
async def test_lookup_returns_found_with_snippet_around_price() -> None:
    chunk = RagChunk(
        id=42,
        source_id="kb:1",
        chunk_text=(
            "Тур длится 6 часов и проходит по горным тропам. "
            "Стоимость тура — 15 000 ₽ за группу до четырёх человек. "
            "Включён инструктор, экипировка и обед."
        ),
        score=0.9,
        project_id=1,
    )
    lookup, retriever = _lookup([chunk])
    intent = Intent(headcount=4, vehicle_count=1)

    result = await lookup.lookup(
        project_id=1, intent=intent, question="Сколько стоит 6 часов?"
    )

    assert isinstance(result, PriceFound)
    assert result.source_chunk_id == "42"
    assert "15 000 ₽" in result.snippet
    # Snippet is bounded — does not contain the long preamble at the start.
    assert "Тур длится" not in result.snippet
    # Retriever call carries the project_id and a query containing the
    # customer's question plus a price anchor lemma.
    assert retriever.calls
    call = retriever.calls[0]
    assert call["project_id"] == 1
    assert "Сколько стоит 6 часов?" in call["query"]
    assert "цена" in call["query"].lower() or "стоимость" in call["query"].lower()


@pytest.mark.asyncio
async def test_first_price_carrying_chunk_wins_even_when_listed_second() -> None:
    # The retriever returned a high-score chunk that mentions the service
    # but no price token. The next chunk carries the actual price. The
    # lookup must skip the first and pick the second.
    decoy = RagChunk(
        id=7,
        source_id="kb:1",
        chunk_text="Каньонинг — спуск по верёвке вдоль водопадов.",
        score=0.95,
        project_id=1,
    )
    real = RagChunk(
        id=11,
        source_id="kb:2",
        chunk_text="Полдня каньонинга — 15 000 ₽ за группу.",
        score=0.4,
        project_id=1,
    )
    lookup, _ = _lookup([decoy, real])

    result = await lookup.lookup(
        project_id=1,
        intent=Intent(),
        question="Сколько стоит каньонинг?",
    )

    assert isinstance(result, PriceFound)
    assert result.source_chunk_id == "11"


@pytest.mark.asyncio
async def test_empty_retrieval_returns_missing_with_verbatim_question() -> None:
    lookup, _ = _lookup([])
    question = "Сколько стоит 6 часов на квадроцикле?"

    result = await lookup.lookup(
        project_id=1, intent=Intent(), question=question
    )

    assert isinstance(result, PriceMissing)
    assert isinstance(result.payload, PriceUnknownPayload)
    assert result.payload.original_question == question
    assert result.payload.service is None
    assert result.payload.vehicle_type is None
    assert result.payload.hours is None


@pytest.mark.asyncio
async def test_only_non_price_chunks_returns_missing() -> None:
    only_descriptions = [
        RagChunk(
            id=1,
            source_id="kb:1",
            chunk_text="Каньонинг — спуск по верёвке.",
            score=0.95,
            project_id=1,
        ),
        RagChunk(
            id=2,
            source_id="kb:2",
            chunk_text="Тур длится около 6 часов.",
            score=0.5,
            project_id=1,
        ),
    ]
    lookup, _ = _lookup(only_descriptions)

    result = await lookup.lookup(
        project_id=1,
        intent=Intent(),
        question="Сколько стоит каньонинг?",
    )

    assert isinstance(result, PriceMissing)


def test_price_unknown_payload_as_dict_shape() -> None:
    payload = PriceUnknownPayload(
        service=None,
        vehicle_type=None,
        hours=None,
        original_question="Сколько стоит 6 часов?",
    )
    assert payload.as_dict() == {
        "service": None,
        "vehicle_type": None,
        "hours": None,
        "original_question": "Сколько стоит 6 часов?",
    }


# --- N2 (round 8): the quoted price must match the ASKED service -------------
# Live miss: «Сколько стоит … багги?» → «13 000 ₽ за квадроцикл» — the lookup
# returned the first price-bearing chunk regardless of which service it named.

_QUADBIKE_PRICE = "Прокат квадроцикла — 13 000 ₽ за квадроцикл в час."
_BUGGY_PRICE = "Багги — 90 000 ₽ за маршрут, до 4 человек."
_SERVICES = ["Багги", "Квадроцикл"]


@pytest.mark.asyncio
async def test_skips_price_chunk_for_a_service_not_asked_about() -> None:
    lookup, _ = _lookup(
        [RagChunk(id=1, source_id="kb", chunk_text=_QUADBIKE_PRICE, score=0.9)]
    )
    result = await lookup.lookup(
        project_id=1,
        intent=Intent(),
        question="Сколько стоит покататься на багги?",
        service_names=_SERVICES,
    )
    assert isinstance(result, PriceMissing)  # not the квадроцикл price
    assert result.payload.service == "Багги"


@pytest.mark.asyncio
async def test_returns_price_chunk_matching_the_asked_service() -> None:
    lookup, _ = _lookup(
        [
            RagChunk(id=1, source_id="kb", chunk_text=_QUADBIKE_PRICE, score=0.9),
            RagChunk(id=2, source_id="kb", chunk_text=_BUGGY_PRICE, score=0.8),
        ]
    )
    result = await lookup.lookup(
        project_id=1,
        intent=Intent(),
        question="Сколько стоит покататься на багги?",
        service_names=_SERVICES,
    )
    assert isinstance(result, PriceFound)
    assert "90 000" in result.snippet
    assert "13 000" not in result.snippet


@pytest.mark.asyncio
async def test_asked_quadbike_returns_quadbike_price() -> None:
    lookup, _ = _lookup(
        [
            RagChunk(id=1, source_id="kb", chunk_text=_BUGGY_PRICE, score=0.9),
            RagChunk(id=2, source_id="kb", chunk_text=_QUADBIKE_PRICE, score=0.8),
        ]
    )
    result = await lookup.lookup(
        project_id=1,
        intent=Intent(),
        question="Сколько стоит квадроцикл?",
        service_names=_SERVICES,
    )
    assert isinstance(result, PriceFound)
    assert "13 000" in result.snippet


@pytest.mark.asyncio
async def test_no_service_named_keeps_first_price_chunk() -> None:
    # Generic ask with no service mentioned → first price chunk (unchanged).
    lookup, _ = _lookup(
        [RagChunk(id=1, source_id="kb", chunk_text=_QUADBIKE_PRICE, score=0.9)]
    )
    result = await lookup.lookup(
        project_id=1,
        intent=Intent(),
        question="Сколько это стоит?",
        service_names=_SERVICES,
    )
    assert isinstance(result, PriceFound)
    assert "13 000" in result.snippet


# --- N2 round 10: multi-word configured service name -------------------------
# Live service is «Аренда багги» (multi-word); the customer says just «багги».
# The old subset match required ALL name lemmas in the question and failed.

_MULTIWORD_SERVICES = ["Аренда багги", "Квадроцикл"]


@pytest.mark.asyncio
async def test_multiword_service_resolves_from_short_customer_word() -> None:
    lookup, _ = _lookup(
        [
            RagChunk(id=1, source_id="kb", chunk_text=_QUADBIKE_PRICE, score=0.9),
            RagChunk(id=2, source_id="kb", chunk_text=_BUGGY_PRICE, score=0.8),
        ]
    )
    result = await lookup.lookup(
        project_id=1,
        intent=Intent(),
        question="Сколько стоит покататься на багги?",
        service_names=_MULTIWORD_SERVICES,
    )
    assert isinstance(result, PriceFound)
    assert "90 000" in result.snippet  # the багги price
    assert "13 000" not in result.snippet  # never the квадроцикл price


@pytest.mark.asyncio
async def test_multiword_service_no_match_misses_not_wrong_service() -> None:
    lookup, _ = _lookup(
        [RagChunk(id=1, source_id="kb", chunk_text=_QUADBIKE_PRICE, score=0.9)]
    )
    result = await lookup.lookup(
        project_id=1,
        intent=Intent(),
        question="Сколько стоит покататься на багги?",
        service_names=_MULTIWORD_SERVICES,
    )
    assert isinstance(result, PriceMissing)  # not the квадроцикл price
    assert result.payload.service == "Аренда багги"


# --- Story 12.53 (round-11 N2): subject-mismatch escalation, no services -----
# Live: the project has NO configured services and the RAG holds only quadbike
# prices. A "багги" ask must escalate (PriceMissing), never quote the quadbike
# price — even with empty service_names (12.49's guard is inert there).


@pytest.mark.asyncio
async def test_no_services_buggy_ask_does_not_quote_quadbike() -> None:
    lookup, _ = _lookup(
        [RagChunk(id=1, source_id="kb", chunk_text=_QUADBIKE_PRICE, score=0.9)]
    )
    result = await lookup.lookup(
        project_id=1,
        intent=Intent(),
        question="Сколько стоит покататься на багги?",
        service_names=(),  # live reality: nothing configured
    )
    assert isinstance(result, PriceMissing)  # escalate, never mis-quote


@pytest.mark.asyncio
async def test_no_services_generic_ask_returns_first_price() -> None:
    # No subject noun named → no mismatch to detect → first price chunk wins.
    lookup, _ = _lookup(
        [RagChunk(id=1, source_id="kb", chunk_text=_QUADBIKE_PRICE, score=0.9)]
    )
    result = await lookup.lookup(
        project_id=1, intent=Intent(), question="Сколько это стоит?", service_names=()
    )
    assert isinstance(result, PriceFound)


@pytest.mark.asyncio
async def test_no_services_subject_match_returns_price() -> None:
    lookup, _ = _lookup(
        [RagChunk(id=1, source_id="kb", chunk_text=_BUGGY_PRICE, score=0.9)]
    )
    result = await lookup.lookup(
        project_id=1, intent=Intent(), question="Сколько стоит багги?", service_names=()
    )
    assert isinstance(result, PriceFound)  # chunk is about багги → quote it
