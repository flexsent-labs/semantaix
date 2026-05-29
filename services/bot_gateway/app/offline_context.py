"""Pure helpers for offline backlog recovery: staleness + context heuristics.

Kept free of I/O (except the one-shot cue-file load) so every branch is
trivially unit-testable with an injected ``now`` and explicit thresholds —
the project requires an injected clock, never ambient ``datetime.now()`` in
branch logic.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def is_stale(
    *,
    message_date: int | None,
    now: datetime,
    stale_seconds: float,
) -> bool:
    """True when a message is old enough to be redelivered backlog.

    ``message_date`` is Telegram's send time (Unix epoch seconds). ``None``
    means the payload omitted/garbled it — treated as a live message so the
    current immediate-forward path is preserved.
    """
    if message_date is None:
        return False
    try:
        sent_at = datetime.fromtimestamp(message_date, tz=UTC)
    except (OverflowError, OSError, ValueError):
        # Corrupt/out-of-range epoch from an untrusted payload — never treat
        # it as backlog (that would buffer + delay a possibly-live message).
        return False
    return (now - sent_at).total_seconds() > stale_seconds


def is_thin(
    text: str,
    *,
    min_context_chars: int,
    cues: frozenset[str],
) -> bool:
    """True when ``text`` cannot stand alone and needs preceding context.

    Thin = shorter than ``min_context_chars`` OR a known short context cue
    (exact match, or its first word is a cue — e.g. "да", "сколько?",
    "а это?").
    """
    stripped = text.strip()
    if len(stripped) < min_context_chars:
        return True
    words = _WORD_RE.findall(stripped.lower())
    if not words:
        return True
    if " ".join(words) in cues:
        return True
    return words[0] in cues


def build_inbound_text(*, latest: str, preceding: list[str]) -> str:
    """Compose the single inbound text answered for a flushed backlog.

    With no preceding context, the latest message is forwarded verbatim.
    Otherwise the preceding messages are labelled as context so the answer
    pipeline can tell them apart from the actual question.
    """
    if not preceding:
        return latest
    context_lines = "\n".join(f"- {message}" for message in preceding)
    return (
        "Предыдущие сообщения (контекст):\n"
        f"{context_lines}\n"
        f"Вопрос клиента: {latest}"
    )


@lru_cache(maxsize=1)
def _load_cues(path: str) -> frozenset[str]:
    raw = Path(path).read_text(encoding="utf-8")
    cues: set[str] = set()
    for line in raw.splitlines():
        stripped = line.strip().lower()
        if not stripped or stripped.startswith("#"):
            continue
        cues.add(stripped)
    return frozenset(cues)


def _default_cues_path() -> str:
    return str(
        Path(__file__).resolve().parents[3] / "data" / "russian_context_cues.txt"
    )


def load_context_cues(path: str | None = None) -> frozenset[str]:
    return _load_cues(path or _default_cues_path())
