"""Price lookup for the sales `pricing` turn (Story 12.04).

`PriceLookup` is a thin layer over the existing lemma-overlap RAG retriever:
it builds a price-flavoured query from the customer's question + a few
fixed price anchor lemmas, scores returned chunks against a strict
``digit-then-currency`` regex, and returns either:

  * ``PriceFound`` — the chunk text, its source chunk id (for trace), and
    a ±60-char snippet around the price token (so the LLM has just
    enough context to quote the price verbatim).
  * ``PriceMissing`` — a structured ``PriceUnknownPayload`` carrying the
    customer's verbatim question. When the active funnel already selected a
    service, that canonical service is retained in the payload; the remaining
    ``vehicle_type`` / ``hours`` fields are reserved for later extensions.

The class is intentionally framework-free: callers pass a normalizer and
a `RagRetriever` duck-type. The lookup is async at the call boundary
(the retriever itself is sync sqlite — we hop a thread).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from services.api.app.rag import RagChunk
from services.api.app.sales.intent import Intent

_PRICE_TOKEN_RE = re.compile(
    r"\d[\d\s]*(?:₽|руб(?:\.|лей|ля|ль)?|р\.?|RUB|P)(?![A-Za-zА-Яа-я])",
    flags=re.IGNORECASE,
)

_PRICE_ANCHOR_LEMMAS: tuple[str, ...] = ("цена", "стоимость", "рубль", "₽")

# Story 12.53 (round-11 N2) — generic price/booking/unit/function lemmas that
# carry no SUBJECT meaning. Removing them from a question leaves only the
# distinctive subject nouns (a vehicle/activity like «багги», «квадроцикл»,
# «каньонинг»), so an off-subject price chunk can be told apart from a matching
# one even when NO services are configured. Tunable like the guardrail lists; a
# missing word only risks a (safe) escalation, never a wrong quote — so keep
# distinctive subject nouns OUT of this set.
_PRICE_GENERIC_LEMMAS: frozenset[str] = frozenset(
    {
        # price words
        "цена", "стоимость", "стоить", "рубль", "руб", "₽", "сколько",
        "почём", "почем", "обойтись", "выйти", "ценник",
        # booking / activity-generic
        "тур", "прокат", "аренда", "покататься", "кататься", "поездка",
        "поехать", "услуга", "вариант", "заказать", "забронировать", "бронь",
        "маршрут", "опция",
        # units / quantities
        "час", "часовой", "человек", "группа", "день", "сутки", "минута",
        "время", "штука", "всё", "весь",
        # function words / fillers
        "это", "этот", "на", "в", "за", "до", "с", "со", "по", "и", "или",
        "а", "но", "не", "нужно", "хотеть", "мочь", "быть", "ваш", "наш",
        "я", "мы", "вы", "он", "что", "как",
    }
)

_SNIPPET_RADIUS = 60

# The customer-facing catalog calls enduro rentals "мотоциклы", while the
# approved brochures use "эндуро" or "мото".  Expand only this narrow vehicle
# family so a motorcycle price question can reach the same grounded chunk.
_PRICE_SUBJECT_ALIASES: dict[str, tuple[str, ...]] = {
    "мотоцикл": ("эндуро", "мото", "мопед"),
    "мопед": ("эндуро", "мотоцикл"),
    "эндуро": ("мотоцикл", "мопед"),
}


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


def _best_matching_service(
    text: str, service_names: Sequence[str], normalizer: _Normalizer
) -> str | None:
    """The configured service whose name shares the MOST lemmas with ``text``.

    Max-overlap (Story 12.49, round-10 N2) — so the customer's short «багги»
    resolves to the configured multi-word «Аренда багги» (they share the lemma
    «багга»), and a «квадроцикл» chunk resolves to a different service. Returns
    ``None`` when nothing overlaps (a generic ask names no service → the lookup
    keeps its first-price-chunk behaviour). Running the SAME routine on the
    question and on each chunk makes the match symmetric and robust to shared
    generic words ("аренда" / "прокат"): the distinctive noun gives the max
    overlap, so a chunk is kept only when ITS best-matching service is the asked
    one (12.44's subset match failed entirely on multi-word service names).
    """
    best: str | None = None
    best_overlap = 0
    for name in service_names:
        name_lemmas = {lemma for lemma in normalizer.lemmas(name) if lemma}
        overlap = len(name_lemmas & set(normalizer.lemmas(text)))
        if overlap > best_overlap:
            best, best_overlap = name, overlap
    return best


def _subject_lemmas(text: str, normalizer: _Normalizer) -> set[str]:
    """Distinctive subject lemmas of ``text`` — content nouns left after the
    generic price/booking/unit/function words and bare numbers are removed
    (Story 12.53, round-11 N2). Empty for a generic ask ("сколько стоит?")."""
    return {
        lemma
        for lemma in normalizer.lemmas(text)
        if lemma and lemma not in _PRICE_GENERIC_LEMMAS and not lemma.isdigit()
    }


def _expand_price_subject(subject: set[str]) -> set[str]:
    expanded = set(subject)
    for lemma in tuple(subject):
        expanded.update(_PRICE_SUBJECT_ALIASES.get(lemma, ()))
    return expanded


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
        service_names: Sequence[str] = (),
        service_hint: str | None = None,
    ) -> PriceFound | PriceMissing:
        # A mid-booking "а сколько это стоит?" omits the service because it is
        # already established by the funnel. Add that context for retrieval,
        # but keep the original customer wording in a miss payload.
        lookup_question = question
        if service_hint and not _subject_lemmas(question, self._normalizer):
            lookup_question = f"{question} {service_hint}"
        subject = _subject_lemmas(lookup_question, self._normalizer)
        aliases = {
            alias
            for lemma in subject
            for alias in _PRICE_SUBJECT_ALIASES.get(lemma, ())
        }
        query = _build_query(" ".join((lookup_question, *sorted(aliases))))
        chunks = await asyncio.to_thread(
            self._rag.retrieve,
            query=query,
            limit=5,
            project_id=project_id,
        )
        # Story 12.44 (round-8 N2) — never quote a price for a service the
        # customer didn't ask about. When the question names a configured service
        # ("… багги?"), require the winning chunk to mention that same service;
        # a generic ask (no service named) keeps the first-price-chunk behaviour.
        asked = _best_matching_service(lookup_question, service_names, self._normalizer)
        # Story 12.53 (round-11 N2) — the live project has NO services, so `asked`
        # is None and the guard above is inert. Fall back to a catalog-free check:
        # when the customer names a subject ("… багги?"), the winning chunk must
        # mention it, else escalate — never quote an off-subject («квадроцикл»)
        # price. A generic ask names no subject → first-price-chunk behaviour.
        subject = _expand_price_subject(subject)
        for chunk in chunks:
            if not _has_price_token(chunk.chunk_text):
                continue
            if (
                asked is not None
                and _best_matching_service(
                    chunk.chunk_text, service_names, self._normalizer
                )
                != asked
            ):
                continue
            if (
                asked is None
                and subject
                and not (subject & set(self._normalizer.lemmas(chunk.chunk_text)))
            ):
                continue
            snippet = _price_snippet(chunk.chunk_text)
            return PriceFound(
                text=chunk.chunk_text,
                source_chunk_id=str(chunk.id),
                snippet=snippet,
            )
        return PriceMissing(
            payload=PriceUnknownPayload(
                service=asked or service_hint,
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
