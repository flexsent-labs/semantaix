"""Parse a free-form Russian date span into a start date.

The proactive-followup job (Story 12.08) needs a single yes/no decision:
is the customer's stated tour date already in the past? The parser is
intentionally narrow — it covers the shapes the scoping LLM emits in
practice ("1 мая", "20 апреля", "10-15 июня", "с 1 по 5 мая",
"20.04.2026", "20.04") — and returns ``None`` when nothing matches so
callers can default to "not stale, keep the nudge".

When the year is omitted, the current year (per ``today``) is used; we do
not roll forward into next year, because the staleness check wants to
detect dates that already lapsed.
"""

from __future__ import annotations

import re
from datetime import date

_MONTH_TOKENS: dict[str, int] = {
    "январ": 1,
    "феврал": 2,
    "март": 3,
    "апрел": 4,
    "мая": 5,
    "май": 5,
    "июн": 6,
    "июл": 7,
    "август": 8,
    "сентябр": 9,
    "октябр": 10,
    "ноябр": 11,
    "декабр": 12,
}


_NUMERIC_RE = re.compile(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?")
_DAY_RE = re.compile(r"\d{1,2}")
_WORD_RE = re.compile(r"[а-яА-ЯёЁ]+", re.UNICODE)


def _month_from_token(token: str) -> int | None:
    lowered = token.lower()
    for prefix, month in _MONTH_TOKENS.items():
        if lowered.startswith(prefix):
            return month
    return None


def parse_russian_date_span(text: str | None, *, today: date) -> date | None:
    """Return the start date of the customer's stated span, or ``None``.

    ``today`` provides the year fallback (no implicit "next-year"
    rollover — see module docstring).
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped:
        return None

    numeric = _NUMERIC_RE.search(stripped)
    if numeric is not None:
        day_str, month_str, year_str = numeric.groups()
        try:
            day = int(day_str)
            month = int(month_str)
        except ValueError:  # pragma: no cover — regex guarantees digits
            return None
        year = today.year
        if year_str:
            year_int = int(year_str)
            year = year_int if year_int >= 100 else 2000 + year_int
        return _safe_date(year=year, month=month, day=day)

    month: int | None = None
    month_pos: int | None = None
    for match in _WORD_RE.finditer(stripped):
        candidate = _month_from_token(match.group(0))
        if candidate is not None:
            month = candidate
            month_pos = match.start()
            break
    if month is None or month_pos is None:
        return None

    first_day_match = _DAY_RE.search(stripped, 0, month_pos)
    if first_day_match is None:
        return None
    try:
        day = int(first_day_match.group(0))
    except ValueError:  # pragma: no cover
        return None
    return _safe_date(year=today.year, month=month, day=day)


def _safe_date(*, year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None
