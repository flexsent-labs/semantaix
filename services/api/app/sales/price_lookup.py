"""Price lookup for the sales `pricing` turn (Story 12.04).

`PriceLookup` is a thin layer over the existing lemma-overlap RAG retriever:
it builds a price-flavoured query from the customer's question + a few
fixed price anchor lemmas, scores returned chunks against a strict
``digit-then-currency`` regex, and returns either:

  * ``PriceFound`` — the chunk text, its source chunk id (for trace), and
    a ±60-char snippet around the price token (so the LLM has just
    enough context to quote the price verbatim).
  * ``PriceMissing`` — a structured ``PriceUnknownPayload`` carrying the
    customer's verbatim question. The ``service`` / ``vehicle_type`` /
    ``hours`` fields are reserved for later stories that extend
    ``Intent`` with those tags; today they are always ``None``.

The class is intentionally framework-free: callers pass a normalizer and
a `RagRetriever` duck-type. The lookup is async at the call boundary
(the retriever itself is sync sqlite — we hop a thread).
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from services.api.app.rag import RagChunk
from services.api.app.sales.intent import Intent

_PRICE_TOKEN_RE = re.compile(
    r"\d[\d\s]*(?:₽|руб(?:\.|лей|ля|ль)?|р\.|RUB)",
    flags=re.IGNORECASE,
)

_PRICE_ANCHOR_LEMMAS: tuple[str, ...] = ("цена", "стоимость", "рубль", "₽")

_SNIPPET_RADIUS = 60


class _Normalizer(Protocol):
    def lemmas(self, text: str) -> list[str]: ...


class _RagRetriever(Protocol):
    def retrieve(
        self,
        *,
        query: str,
        limit: int = 3,
        project_id: int | None = None,
    ) -> list[RagChunk]: ...


@dataclass(frozen=True)
class PriceUnknownPayload:
    """Structured payload attached to ``reason='price_unknown'`` tickets.

    ``original_question`` is the customer's verbatim text — never paraphrase.
    The other three fields stay ``None`` until later stories extend
    ``Intent`` with explicit ``service``/``vehicle_type``/``hours`` tags;
    keeping them in the schema today gives the operator-facing payload a
    stable shape across the epic.
    """

    service: str | None
    vehicle_type: str | None
    hours: int | None
    original_question: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PriceFound:
    text: str
    source_chunk_id: str
    snippet: str


@dataclass(frozen=True)
class PriceMissing:
    payload: PriceUnknownPayload


def _has_price_token(text: str) -> bool:
    return bool(_PRICE_TOKEN_RE.search(text))


def _price_snippet(text: str) -> str:
    match = _PRICE_TOKEN_RE.search(text)
    assert match is not None  # called only when _has_price_token is True
    start = max(0, match.start() - _SNIPPET_RADIUS)
    end = min(len(text), match.end() + _SNIPPET_RADIUS)
    return text[start:end].strip()


def _build_query(question: str) -> str:
    """Compose a lemma-space query from the question + fixed price anchors.

    The retriever already lemmatises both sides, so the customer's exact
    inflection is preserved while the anchors ensure a price-bearing
    chunk scores higher than a service-only chunk for the same question.
    """
    parts: list[str] = []
    if isinstance(question, str) and question.strip():
        parts.append(question.strip())
    parts.extend(_PRICE_ANCHOR_LEMMAS)
    return " ".join(parts)


class PriceLookup:
    """Resolve a customer's price ask against the RAG knowledge base.

    The lookup is intentionally a single retrieval call (no retries). A
    chunk wins only when it carries a digit-then-currency token — chunks
    that mention the service without quoting a price are excluded so the
    bot never quotes a non-price line.
    """

    def __init__(
        self,
        *,
        rag_retriever: _RagRetriever,
        normalizer: _Normalizer,
    ) -> None:
        self._rag = rag_retriever
        self._normalizer = normalizer

    async def lookup(
        self,
        *,
        project_id: int | None,
        intent: Intent,
        question: str,
    ) -> PriceFound | PriceMissing:
        query = _build_query(question)
        chunks = await asyncio.to_thread(
            self._rag.retrieve,
            query=query,
            limit=5,
            project_id=project_id,
        )
        for chunk in chunks:
            if _has_price_token(chunk.chunk_text):
                snippet = _price_snippet(chunk.chunk_text)
                return PriceFound(
                    text=chunk.chunk_text,
                    source_chunk_id=str(chunk.id),
                    snippet=snippet,
                )
        return PriceMissing(
            payload=PriceUnknownPayload(
                service=None,
                vehicle_type=None,
                hours=None,
                original_question=question,
            )
        )


def extract_price_tokens(text: str) -> list[str]:
    """Return every digit-then-currency token in ``text`` (verbatim spans)."""
    return [match.group(0) for match in _PRICE_TOKEN_RE.finditer(text)]


__all__ = [
    "PriceFound",
    "PriceLookup",
    "PriceMissing",
    "PriceUnknownPayload",
    "extract_price_tokens",
]
