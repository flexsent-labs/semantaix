"""Closure detection for the ``pitching`` / completion stage.

Lemma-based, no LLM — a sibling of :mod:`acceptance`. We read a short list
of "nothing more" phrases from ``data/russian_sales_closure.txt`` and match
their normalized lemmas against the customer's reply, so "всё" /
"больше ничего" / "нет" survive inflection and slang.

A single-word closure phrase must occur as a lemma in the customer's reply.
Multi-word phrases require all of their lemmas, so a shared function word such
as ``на`` from ``на этом всё`` cannot close an unrelated request. This is
consulted ONLY once scoping is complete (the ``pitching`` stage) — never
mid-scoping, where a bare "нет" could be a field answer rather than a closure
signal.
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
    for phrase in phrases:
        phrase_lemmas = tuple(dict.fromkeys(normalizer.lemmas(phrase)))
        if not phrase_lemmas:
            continue
        if len(phrase_lemmas) == 1 and phrase_lemmas[0] in lemmas:
            return True
        if len(phrase_lemmas) > 1 and set(phrase_lemmas).issubset(lemmas):
            return True
    return False


__all__ = ["is_no_more", "load_closure_phrases"]
