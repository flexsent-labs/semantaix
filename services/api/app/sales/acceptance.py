"""Customer-acceptance detection for the ``proposing`` stage (Story 12.07).

Lemma-based, no LLM. We read a short list of acceptance lemmas from
``data/russian_sales_acceptance.txt`` and match by **single-lemma
overlap** against the customer's normalized reply — every phrase in the
file is a single token (``да``, ``согласен``, …), so multi-word lemma
matching isn't needed here.

A non-empty lemma intersection between the file and the reply means the
customer accepted; otherwise the stage handler treats the reply as a
counter-offer / decline.
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
        / "russian_sales_acceptance.txt"
    )


@lru_cache(maxsize=4)
def load_acceptance_phrases(path: str | None = None) -> tuple[str, ...]:
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


def is_acceptance(
    text: str,
    *,
    normalizer: _Normalizer,
    phrases_path: str | None = None,
) -> bool:
    """True iff the customer's reply contains a known acceptance lemma."""
    if not text or not text.strip():
        return False
    phrases = load_acceptance_phrases(phrases_path)
    if not phrases:
        return False
    lemmas = set(normalizer.lemmas(text))
    if not lemmas:
        return False
    phrase_lemma_set: set[str] = set()
    for phrase in phrases:
        for token in normalizer.lemmas(phrase):
            phrase_lemma_set.add(token)
    if not phrase_lemma_set:
        return False
    return bool(lemmas & phrase_lemma_set)


__all__ = ["is_acceptance", "load_acceptance_phrases"]
