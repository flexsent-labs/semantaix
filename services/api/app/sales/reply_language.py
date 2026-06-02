"""Detect the customer's language so the persona replies in it (Story 12.45, D8/N3).

The sales persona's LLM-generated lines (greeting / scoping / pricing) are made
to mirror the customer's language by appending a directive to the system prompt.
Detection is a cheap script heuristic — Latin-dominant text is treated as
English, everything else as Russian (the default), so a Russian conversation is
never altered.
"""

from __future__ import annotations

import re

_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)
_LATIN_RE = re.compile(r"[a-z]", re.IGNORECASE)

_EN_REPLY_DIRECTIVE = (
    "\n\nIMPORTANT: the customer is writing in English — reply in English, "
    "keeping the same persona, tone, and rules."
)


def detect_language(text: str | None) -> str:
    """Return ``"en"`` when ``text`` is predominantly Latin script, else ``"ru"``.

    Russian is the default: empty / non-Latin / Cyrillic-dominant text → ``"ru"``.
    """
    if not text:
        return "ru"
    cyrillic = len(_CYRILLIC_RE.findall(text))
    latin = len(_LATIN_RE.findall(text))
    if latin > 0 and latin > cyrillic:
        return "en"
    return "ru"


def reply_language_directive(text: str | None) -> str:
    """A system-prompt suffix telling the LLM to answer in the customer's language.

    Empty for Russian (the default), so Russian prompts are byte-identical and
    unchanged; an English instruction otherwise.
    """
    return _EN_REPLY_DIRECTIVE if detect_language(text) == "en" else ""


def localize(ru: str, en: str, *, language: str) -> str:
    """Pick the customer's-language variant of a deterministic line (Story 12.47).

    The sales persona's *LLM* lines already mirror the customer's language via
    ``reply_language_directive``; its *deterministic* customer-facing constants
    (ask-for-time, busy/free/unverified verdicts, handoff confirmations) were
    Russian-only, so an English thread reverted to Russian the moment one of
    them fired (round-10 N3). Callers pass the per-turn ``ctx.language`` (set
    once from ``detect_language(question)`` at the turn boundary): ``"en"`` →
    the English variant, anything else → the Russian default, so Russian
    conversations are byte-identical and unchanged.
    """
    return en if language == "en" else ru


__all__ = ["detect_language", "localize", "reply_language_directive"]
