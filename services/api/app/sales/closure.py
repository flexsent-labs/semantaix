"""Closure detection for the ``pitching`` / completion stage.

Lemma-based, no LLM — a sibling of :mod:`acceptance`. We read a short list
of "nothing more" lemmas from ``data/russian_sales_closure.txt`` and match
by **single-lemma overlap** against the customer's normalized reply, so
"всё" / "больше ничего" / "нет" survive inflection and slang.

A non-empty lemma intersection means the customer signalled they have
nothing more to add. This is consulted ONLY once scoping is complete (the
``pitching`` stage) — never mid-scoping, where a bare "нет" could be a
field answer rather than a closure signal.
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
        / "russian_sales_closure.txt"
    )


@lru_cache(maxsize=4)
def load_closure_phrases(path: str | None = None) -> tuple[str, ...]:
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


def is_no_more(
    text: str,
    *,
    normalizer: _Normalizer,
    phrases_path: str | None = None,
) -> bool:
    """True iff the customer's reply contains a known closure lemma."""
    if not text or not text.strip():
        return False
    phrases = load_closure_phrases(phrases_path)
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


__all__ = ["is_no_more", "load_closure_phrases"]
