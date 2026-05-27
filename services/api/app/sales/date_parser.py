"""Russian date-span parser for sales date proposals (Story 12.07).

Translates the free-text date strings the scoping LLM emits (and the
counter-offers the customer types during the ``proposing`` stage) into a
typed ``(start_date, end_date)`` window the date proposer can feed into
Epic 11's availability engine.

Shapes accepted:
- ``"1 мая"``       → (May 1, May 1)
- ``"15 июня"``     → (June 15, June 15)
- ``"1–3 мая"``     → (May 1, May 3)
- ``"1-3 мая"``     → (May 1, May 3)
- ``"в мае"``       → (May 1, May 31)  (whole-month span)

Anything else returns ``None`` so the answerer can ask a scoping
clarification. The year falls back to ``now.year`` — we never roll
forward into the following year on the parser side; that decision
belongs to the availability engine.
"""

from __future__ import annotations

import calendar
import re
from datetime import date

# Russian month prefixes (sufficient to cover every grammatical case for
# the shapes the parser accepts). Lookup is by ``startswith``, so a single
# prefix like ``"январ"`` matches ``"января"`` / ``"январе"`` / ``"январь"``.
_MONTH_PREFIXES: tuple[tuple[str, int], ...] = (
    ("январ", 1),
    ("феврал", 2),
    ("март", 3),
    ("апрел", 4),
    ("мая", 5),
    ("мае", 5),
    ("май", 5),
    ("июн", 6),
    ("июл", 7),
    ("август", 8),
    ("сентябр", 9),
    ("октябр", 10),
    ("ноябр", 11),
    ("декабр", 12),
)

_RANGE_RE = re.compile(
    r"(\d{1,2})\s*[-–]\s*(\d{1,2})\s+([а-яё]+)",
    re.UNICODE | re.IGNORECASE,
)
_SINGLE_RE = re.compile(
    r"(\d{1,2})\s+([а-яё]+)",
    re.UNICODE | re.IGNORECASE,
)
_WHOLE_MONTH_RE = re.compile(
    r"\bв\s+([а-яё]+)",
    re.UNICODE | re.IGNORECASE,
)


def _month_from_token(token: str) -> int | None:
    lowered = token.lower()
    for prefix, month in _MONTH_PREFIXES:
        if lowered.startswith(prefix):
            return month
    return None


def _safe_date(*, year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_russian_date_span(
    text: str | None, *, now: date
) -> tuple[date, date] | None:
    """Return ``(start, end)`` for a recognised Russian date span, else ``None``.

    The year defaults to ``now.year``; the parser does not roll forward
    into next year (that decision is downstream).
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    lowered = stripped.lower()

    range_match = _RANGE_RE.search(lowered)
    if range_match is not None:
        try:
            d1 = int(range_match.group(1))
            d2 = int(range_match.group(2))
        except ValueError:  # pragma: no cover — regex guarantees digits
            return None
        month = _month_from_token(range_match.group(3))
        if month is not None and d1 <= d2:
            start = _safe_date(year=now.year, month=month, day=d1)
            end = _safe_date(year=now.year, month=month, day=d2)
            if start is not None and end is not None:
                return start, end

    single_match = _SINGLE_RE.search(lowered)
    if single_match is not None:
        try:
            day = int(single_match.group(1))
        except ValueError:  # pragma: no cover
            return None
        month = _month_from_token(single_match.group(2))
        if month is not None:
            value = _safe_date(year=now.year, month=month, day=day)
            if value is not None:
                return value, value

    whole_match = _WHOLE_MONTH_RE.search(lowered)
    if whole_match is not None:
        month = _month_from_token(whole_match.group(1))
        if month is not None:
            _, last_day = calendar.monthrange(now.year, month)
            start = _safe_date(year=now.year, month=month, day=1)
            end = _safe_date(year=now.year, month=month, day=last_day)
            if start is not None and end is not None:
                return start, end

    return None


__all__ = ["parse_russian_date_span"]
