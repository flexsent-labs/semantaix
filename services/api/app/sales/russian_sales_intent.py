"""Sales-intent detection (Story 12.03).

Seed phrases live in ``data/russian_sales_intent.txt``. We match by lemma
overlap against the normalized question, so the question survives
inflection ("туры" → "тур", "квадроциклов" → "квадроцикл") and the
slang dictionary already wired into the normalizer.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Protocol


class _Normalizer(Protocol):
    def lemmas(self, text: str) -> list[str]: ...


def _default_phrases_path() -> str:
    return str(
        Path(__file__).resolve().parents[4]
        / "data"
        / "russian_sales_intent.txt"
    )


@lru_cache(maxsize=4)
def load_sales_intent_phrases(path: str | None = None) -> tuple[str, ...]:
    """Read the seed file, trim, drop blanks/comments. Memoised by path."""
    resolved = Path(path) if path else Path(_default_phrases_path())
    raw = resolved.read_text(encoding="utf-8")
    out: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(stripped)
    return tuple(out)


def is_sales_intent(
    text: str,
    *,
    normalizer: _Normalizer,
    phrases_path: str | None = None,
) -> bool:
    """True iff any seed lemma is present in the lemmatised text."""
    if not text or not text.strip():
        return False
    phrases = load_sales_intent_phrases(phrases_path)
    if not phrases:
        return False
    lemmas = set(normalizer.lemmas(text))
    if not lemmas:
        return False
    for phrase in phrases:
        phrase_lemmas = normalizer.lemmas(phrase)
        if not phrase_lemmas:
            continue
        if all(lemma in lemmas for lemma in phrase_lemmas):
            return True
    return False
