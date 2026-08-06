"""Typo-tolerant matching for the service names used by the sales funnel.

The Russian normalizer handles inflection, but a customer message can still
contain a spelling error (for example, ``квадрациклы``).  This module keeps a
small, conservative alias vocabulary and applies a bounded Levenshtein match
to normalized one-word tokens.  Short generic words are exact-match only, so
the fuzzy layer cannot turn an unrelated message into a booking request.
"""

from __future__ import annotations

from typing import Protocol


class _Normalizer(Protocol):
    def lemmas(self, text: str) -> list[str]: ...


# These are service families offered by the current tourism catalogue.  The
# aliases include colloquial forms that customers use in Telegram; inflected
# forms are supplied by RussianNormalizer rather than being listed manually.
_SERVICE_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("багги", ("багги", "баги", "баг")),
    (
        "квадроцикл",
        (
            "квадроцикл",
            "квадрацикл",
            "квадрацыкл",
            "квадроцикал",
            "квадрик",
            "квадро",
        ),
    ),
    ("эндуро", ("эндуро", "эндур")),
    ("снегоход", ("снегоход",)),
    ("мопед", ("мопед", "мотоцикл", "мотик")),
    ("скутер", ("скутер",)),
    ("питбайк", ("питбайк",)),
    ("вездеход", ("вездеход",)),
    ("сигвей", ("сигвей", "гироскутер", "segway")),
    ("велосипед", ("велосипед",)),
    ("треккинг", ("треккинг", "трекинг")),
    ("экспедиция", ("экспедиция",)),
    ("пикник", ("пикник",)),
    ("баня", ("баня",)),
    ("трансфер", ("трансфер",)),
    ("кейтеринг", ("кейтеринг",)),
    ("квест", ("квест",)),
    ("экскурсия", ("экскурсия",)),
    ("комбо", ("комбо",)),
    ("тур", ("тур",)),
)


def _edit_distance(left: str, right: str, *, limit: int) -> int:
    """Return Levenshtein distance, stopping once ``limit`` is exceeded."""
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, start=1):
        current = [row]
        for column, right_char in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (left_char != right_char),
                )
            )
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _max_typo_distance(token: str) -> int:
    """Set a conservative typo budget based on token length.

    Words of four letters or fewer are exact-match only.  Longer service names
    may tolerate one edit; very long names tolerate two, which covers the
    common ``о/а`` and ``и/ы`` errors in ``квадроцикл`` without making generic
    Russian words service aliases.
    """
    if len(token) <= 4:
        return 0
    return 2 if len(token) >= 8 else 1


def _normalized_service_aliases(normalizer: _Normalizer) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(
            lemma
            for alias in aliases
            for lemma in normalizer.lemmas(alias)
            if lemma
        )
        for _canonical, aliases in _SERVICE_ALIASES
    )


def matched_service_groups(
    text: str, *, normalizer: _Normalizer
) -> frozenset[str]:
    """Return canonical service groups mentioned in ``text``.

    Matching is performed against normalizer lemmas, so both inflection and a
    bounded number of spelling errors are accepted.  Only single-token aliases
    are considered; this keeps the detector suitable for the early activation
    gate and avoids broad substring matches.
    """
    if not text or not text.strip():
        return frozenset()
    text_lemmas = tuple(dict.fromkeys(normalizer.lemmas(text)))
    if not text_lemmas:
        return frozenset()

    matches: set[str] = set()
    for (canonical, _aliases), normalized_aliases in zip(
        _SERVICE_ALIASES, _normalized_service_aliases(normalizer), strict=True
    ):
        for token in text_lemmas:
            for alias in normalized_aliases:
                limit = _max_typo_distance(alias)
                if _edit_distance(token, alias, limit=limit) <= limit:
                    matches.add(canonical)
                    break
            if canonical in matches:
                break
    return frozenset(matches)


def contains_offered_service(text: str, *, normalizer: _Normalizer) -> bool:
    """True when ``text`` names at least one supported service family."""
    return bool(matched_service_groups(text, normalizer=normalizer))


__all__ = ["contains_offered_service", "matched_service_groups"]
