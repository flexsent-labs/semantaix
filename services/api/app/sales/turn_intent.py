"""Per-turn intent classifier for `SalesPersonaAnswerer` (Story 12.06).

The classifier runs at the top of every active sales stage (scoping /
pitching / pricing) so two conversational asides — *"что у вас есть?"*
and *"что такое X?"* — can be answered inline without disturbing the
funnel state. Anything that does not match a known trigger returns
``TurnIntent(kind="other")`` and the stage handler decides whether to
treat the turn as its open question's answer.

Matching uses three layers, in priority order:

  1. **Catalog ask** — lemma-overlap against a small list of catalog
     phrases. Wins over concept on phrases like *"какие туры есть?"*
     where both lists could otherwise match.
  2. **Concept ask** — lemma-anchored trigger ("что такое", "что
     значит", "объясните", "расскажите про", "расскажите о"). The
     candidate term is the **literal substring** following the trigger
     up to a sentence-terminator (``? ! .``), an em-dash, or end of
     input. When the span is empty (e.g. *"Что такое?"*), the kind
     downgrades to ``other`` — guessing is worse than asking.
  3. **Price ask** — small lemma list (``цена``, ``стоимость``,
     ``сколько стоит``). Used by story 12.04 to route pricing turns.

The classifier never produces ``scoping_answer`` — that decision is the
stage handler's job (when the classifier returns ``other`` *and* the
state is ``scoping``, the turn is treated as a scoping reply).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol


class _Normalizer(Protocol):
    def lemmas(self, text: str) -> list[str]: ...


@dataclass(frozen=True)
class TurnIntent:
    """Classifier output. ``term`` is populated only for ``concept_ask``."""

    kind: str
    term: str | None = None


_CATALOG_PHRASES: tuple[tuple[str, ...], ...] = tuple(
    tuple(phrase.split())
    for phrase in (
        "что у вас есть",
        "какие туры",
        "какие у вас туры",
        "что вы предлагаете",
        "что предлагаете",
        "варианты",
        "список",
        "ассортимент",
    )
)

# Concept triggers ordered longest-first so the literal-substring search
# matches "расскажите про" before "расскажите о".
_CONCEPT_TRIGGERS: tuple[str, ...] = (
    "что такое",
    "что значит",
    "расскажите про",
    "расскажите о",
    "объясните",
)

_PRICE_LEMMAS: frozenset[str] = frozenset({"цена", "стоимость", "почём"})
_PRICE_PHRASES: tuple[tuple[str, ...], ...] = tuple(
    tuple(phrase.split()) for phrase in ("сколько стоить",)
)

# Sentence terminators / dashes that bound the concept-term span.
_TERM_BOUNDARY_RE = re.compile(r"[?!\.…\n—–]")


def _lemmas_contain_phrase(
    lemmas: list[str], phrase_lemmas: tuple[str, ...]
) -> bool:
    """True iff every lemma in ``phrase_lemmas`` is present in ``lemmas``.

    Callers must pass a non-empty ``phrase_lemmas`` — the public helpers
    above already guard against empty targets.
    """
    lemma_set = set(lemmas)
    return all(token in lemma_set for token in phrase_lemmas)


def _phrase_lemmas(
    phrase_tokens: tuple[str, ...], normalizer: _Normalizer
) -> tuple[str, ...]:
    return tuple(normalizer.lemmas(" ".join(phrase_tokens)))


def _is_catalog_ask(text: str, normalizer: _Normalizer) -> bool:
    lemmas = normalizer.lemmas(text)
    if not lemmas:
        return False
    for phrase_tokens in _CATALOG_PHRASES:
        target = _phrase_lemmas(phrase_tokens, normalizer)
        if target and _lemmas_contain_phrase(lemmas, target):
            return True
    return False


def _extract_concept_term(text: str) -> tuple[str | None, bool]:
    """Return ``(term, matched_trigger)`` for the first concept trigger.

    ``matched_trigger`` is ``True`` whenever a trigger phrase appeared in
    the text — even when the trailing span is empty (the caller then
    downgrades the intent to ``other``). The term is the literal
    substring after the trigger, trimmed to the first sentence
    terminator / dash. Returns ``(None, False)`` when no trigger fires.
    """
    lowered = text.lower()
    earliest_match: tuple[int, int] | None = None  # (start, end_of_trigger)
    for trigger in _CONCEPT_TRIGGERS:
        idx = lowered.find(trigger)
        if idx == -1:
            continue
        end = idx + len(trigger)
        if earliest_match is None or idx < earliest_match[0]:
            earliest_match = (idx, end)
    if earliest_match is None:
        return None, False

    _, trigger_end = earliest_match
    tail = text[trigger_end:]
    boundary = _TERM_BOUNDARY_RE.search(tail)
    span = tail[: boundary.start()] if boundary is not None else tail
    span = span.strip(" \t,;:")
    if not span:
        return None, True
    # An all-punctuation span (e.g. trailing "...") leaves nothing useful.
    if not any(ch.isalnum() for ch in span):
        return None, True
    return span, True


def _is_price_ask(text: str, normalizer: _Normalizer) -> bool:
    lemmas = normalizer.lemmas(text)
    if not lemmas:
        return False
    if _PRICE_LEMMAS & set(lemmas):
        return True
    for phrase_tokens in _PRICE_PHRASES:
        target = _phrase_lemmas(phrase_tokens, normalizer)
        if target and _lemmas_contain_phrase(lemmas, target):
            return True
    return False


def classify_turn(text: str, *, normalizer: _Normalizer) -> TurnIntent:
    """Classify a customer turn into the smallest useful intent bucket.

    Priority: ``catalog_ask`` → ``concept_ask`` → ``price_ask`` →
    ``other``. The catalog branch wins when both shapes match, so
    *"какие туры?"* is a catalog ask rather than a concept ask about
    "туры".
    """
    if not text or not text.strip():
        return TurnIntent(kind="other")

    if _is_catalog_ask(text, normalizer):
        return TurnIntent(kind="catalog_ask")

    term, matched_trigger = _extract_concept_term(text)
    if matched_trigger:
        if term is None:
            return TurnIntent(kind="other")
        return TurnIntent(kind="concept_ask", term=term)

    if _is_price_ask(text, normalizer):
        return TurnIntent(kind="price_ask")

    return TurnIntent(kind="other")


__all__ = ["TurnIntent", "classify_turn"]
