"""Mid-scoping decline detection — "this field doesn't apply / not needed".

Lemma-based, no LLM (sibling of :mod:`closure`). Distinguishes a genuine
decline ("не нужно", "нет", "0", "без водителей") from a real field answer
that merely contains a negation ("нет, троих" → still captures the count).

The rule: drop field-name / "need" filler lemmas, then every remaining
content lemma must be a negation or a zero word. Any leftover value lemma
("трое", "знать") means it is **not** a pure decline, so a real answer is
never swallowed.
"""

from __future__ import annotations

from typing import Protocol


class _Normalizer(Protocol):
    def lemmas(self, text: str) -> list[str]: ...


# Negation / "none" lemma roots (pymorphy3 normal forms).
_NEGATION: frozenset[str] = frozenset(
    {"не", "нет", "нету", "без", "нисколько", "нельзя", "никакой", "никак"}
)
_ZERO: frozenset[str] = frozenset({"0", "ноль"})
# Lemmas that carry no field VALUE — they pair with a negation ("не нужно")
# or merely name the field being declined ("без водителей"). Dropping them
# lets a bare value lemma ("трое") still register as a real answer.
_FILLER: frozenset[str] = frozenset(
    {
        "нужно",
        "нужный",
        "надо",
        "требоваться",
        "хотеть",
        "это",
        "штука",
        "водитель",
        "человек",
        "багги",
        "квадроцикл",
        "машина",
        "мотоцикл",
        "дата",
        "день",
        "время",
        "сложность",
        "тур",
        "пожелание",
        # Natural explanatory tails after a decline: "без водителей, мы сами
        # разберёмся" is still a pure refusal of the drivers field.
        "сам",
        "сами",
        "самостоятельно",
        "разобраться",
        "справиться",
        "мы",
        "я",
        "за",
        "руль",
    }
)


def is_decline(text: str, *, normalizer: _Normalizer) -> bool:
    """True iff the reply is a *pure* decline of the field just asked."""
    if not text or not text.strip():
        return False
    content = [
        lemma for lemma in normalizer.lemmas(text) if lemma not in _FILLER
    ]
    if not content:
        return False
    return all(lemma in _NEGATION or lemma in _ZERO for lemma in content)


__all__ = ["is_decline"]
