"""Resolve a configured service + (optionally) a requested time from free
Russian customer text (Epic 11, story 11.06).

Two pure functions, no I/O, deterministic when ``now``/``project_tz`` are
injected:

* :func:`resolve_service` lemma-matches the message against each configured
  ``ServiceRule.name`` (reusing :class:`RussianNormalizer` — razdel + slang +
  pymorphy3, never a parallel tokenizer). Inflected mentions ("на маникюре")
  match the canonical name ("маникюр") because both sides are lemmatized.
  Exactly one matching service → :class:`Resolved`; none → :class:`NoMatch`;
  two or more → :class:`Ambiguous` (so the answerer in 11.07 can ask once,
  then escalate — it never guesses).

* :func:`extract_requested_start` is a deliberately CONSERVATIVE Russian
  date/time extractor. It only commits to a tz-aware ``datetime`` when both an
  explicit day anchor ("сегодня"/"завтра"/"послезавтра"/a named weekday) AND a
  concrete clock time ("в 15:00", "в 3 часа") are present and in range.
  Anything ambiguous or unparseable returns ``None`` so 11.07 clarifies or
  escalates rather than booking the wrong slot.

The clarifying-copy constants below are illustrative defaults; production copy
is configurable as data (project-context: Russian-first content is data).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from services.api.app.russian_text import get_russian_normalizer
from services.api.app.russian_text.normalizer import RussianNormalizer

from .settings_repository import ServiceRule

# --- Illustrative clarifying copy (Russian-first; configurable as data). ----
CLARIFY_NO_SERVICE_NAMED = (
    "Подскажите, пожалуйста, на какую услугу вы хотите записаться?"
)
CLARIFY_NO_MATCH = (
    "Не нашёл такую услугу. Уточните, пожалуйста, что именно вы хотите записать?"
)
CLARIFY_AMBIGUOUS = (
    "Уточните, пожалуйста, какая именно услуга вам нужна: {options}?"
)


@dataclass(frozen=True)
class Resolved:
    """Exactly one configured service matched the message."""

    service: ServiceRule


@dataclass(frozen=True)
class NoMatch:
    """No configured service matched (unknown service or none named)."""


@dataclass(frozen=True)
class Ambiguous:
    """Two or more configured services matched; the customer must disambiguate."""

    candidates: tuple[ServiceRule, ...]


# Discriminated result of :func:`resolve_service`.
ServiceMatch = Resolved | NoMatch | Ambiguous


def resolve_service(
    *,
    text: str,
    service_rules: list[ServiceRule],
    normalizer: RussianNormalizer,
) -> ServiceMatch:
    """Map ``text`` to one of ``service_rules`` by lemma overlap.

    A rule matches when every lemma of its (non-empty) ``name`` appears in the
    lemmatized message — robust to inflection because both sides go through
    :meth:`RussianNormalizer.lemmas`. Rules with a blank/``None`` name can never
    match. Exactly one match → :class:`Resolved`; zero → :class:`NoMatch`; two
    or more → :class:`Ambiguous` (never silently pick one).
    """
    message_lemmas = set(normalizer.lemmas(text))
    matches: list[ServiceRule] = []
    for rule in service_rules:
        if not rule.name or not rule.name.strip():
            continue
        name_lemmas = normalizer.lemmas(rule.name)
        if not name_lemmas:
            continue
        if set(name_lemmas) <= message_lemmas:
            matches.append(rule)

    if len(matches) == 1:
        return Resolved(service=matches[0])
    if len(matches) >= 2:
        return Ambiguous(candidates=tuple(matches))
    return NoMatch()


# --- Conservative Russian date/time extraction. ----------------------------

# Relative day anchors → offset in days from "today" (local project date).
_RELATIVE_DAYS: dict[str, int] = {
    "сегодня": 0,
    "завтра": 1,
    "послезавтра": 2,
}

# Named weekdays (lemmatized form) → Python weekday index (Monday == 0).
_WEEKDAYS: dict[str, int] = {
    "понедельник": 0,
    "вторник": 1,
    "среда": 2,
    "четверг": 3,
    "пятница": 4,
    "суббота": 5,
    "воскресенье": 6,
}

# "в 15:00" / "в 15.00" — explicit hour:minute.
_HH_MM = re.compile(r"\b(\d{1,2})[:.](\d{2})\b")
# "в 3 часа" / "в 15 часов" / "в 9 час" — hour + час-stem, minute defaults to 0.
_HH_CLOCK = re.compile(
    r"\b(\d{1,2})\s*час(?:а|ов|у)?\b",
    re.IGNORECASE | re.UNICODE,
)
# Story 12.20 — bare hour "в 8" / "в 8 утра" / "в 8 вечера" (no colon, no
# час-stem). The optional period word disambiguates AM/PM; a bare hour is the
# literal 24h value.
_HH_BARE = re.compile(
    r"\bв\s+(\d{1,2})\s*(утра|вечера|ночи|дня)?\b",
    re.IGNORECASE | re.UNICODE,
)
# Story 12.20 — ordinal day-of-month: "31-ое" / "31-го" / "10-е" / "10 числа".
_ORDINAL_DAY = re.compile(
    r"\b(\d{1,2})\s*(?:-?(?:ое|ого|го|ой|ый|й|я|е)\b|числа\b)",
    re.IGNORECASE | re.UNICODE,
)


def _apply_period(hour: int, period: str | None) -> int:
    """Map a bare hour + optional period word to a 24h hour (Story 12.20)."""
    if period == "утра":
        return 0 if hour == 12 else hour
    if period in ("вечера", "ночи", "дня"):
        return hour if hour >= 12 else hour + 12
    return hour


def _extract_clock(text: str) -> tuple[int, int] | None:
    """Return ``(hour, minute)`` if exactly one valid clock time is present.

    Conservative: an out-of-range value (hour > 23, minute > 59) yields
    ``None``; the answerer then clarifies instead of guessing.
    """
    hm = _HH_MM.search(text)
    if hm is not None:
        hour, minute = int(hm.group(1)), int(hm.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
        return None
    clock = _HH_CLOCK.search(text)
    if clock is not None:
        hour = int(clock.group(1))
        if 0 <= hour <= 23:
            return hour, 0
        return None
    bare = _HH_BARE.search(text)
    if bare is not None:
        hour = _apply_period(int(bare.group(1)), bare.group(2))
        if 0 <= hour <= 23:
            return hour, 0
        return None
    return None


def _extract_day_offset(lemmas: list[str], today_weekday: int) -> int | None:
    """Resolve a day anchor to a non-negative offset from today, or ``None``.

    Relative words win over weekday names. A named weekday resolves to its next
    occurrence (today counts only when it *is* that weekday). When neither a
    relative word nor a weekday is present, the day is ambiguous → ``None``.
    """
    for lemma in lemmas:
        if lemma in _RELATIVE_DAYS:
            return _RELATIVE_DAYS[lemma]
    for lemma in lemmas:
        target = _WEEKDAYS.get(lemma)
        if target is not None:
            return (target - today_weekday) % 7
    return None


def extract_requested_start(
    *,
    text: str,
    now: datetime,
    project_tz: ZoneInfo,
) -> datetime | None:
    """Best-effort parse of a requested start instant from Russian ``text``.

    Returns a tz-aware ``datetime`` in ``project_tz`` only when BOTH an explicit
    day anchor and a concrete clock time are present and in range; otherwise
    ``None``. Intentionally narrow — it does not attempt relative offsets like
    "через час", bare times without a day, or calendar dates — those return
    ``None`` so 11.07 asks or escalates rather than guessing.

    Lemmatization reuses the shared :class:`RussianNormalizer` singleton (no
    parallel tokenizer); the clock is matched on the raw text since the
    lemmatizer drops the ``:`` separator.
    """
    clock = _extract_clock(text)
    if clock is None:
        return None

    local_now = now.astimezone(project_tz)
    target_date = _extract_target_date(text, local_now)
    if target_date is None:
        return None

    hour, minute = clock
    return datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        hour,
        minute,
        tzinfo=project_tz,
    )


def _extract_target_date(text: str, local_now: datetime) -> date | None:
    """Resolve the requested calendar date (Story 12.20).

    A relative-day word / weekday wins (today + offset); failing that, an
    ordinal day-of-month ("31-ое") rolled forward to its next valid occurrence.
    ``None`` when neither is present.
    """
    lemmas = get_russian_normalizer().lemmas(text)
    offset = _extract_day_offset(lemmas, local_now.weekday())
    if offset is not None:
        return (local_now + timedelta(days=offset)).date()
    return _extract_ordinal_date(text, local_now.date())


def _ordinal_safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _extract_ordinal_date(text: str, today: date) -> date | None:
    """An ordinal day-of-month → the next month (from ``today``) whose calendar
    has that day, on or after today. ``None`` when absent or out of 1..31."""
    match = _ORDINAL_DAY.search(text)
    if match is None:
        return None
    day = int(match.group(1))
    if not 1 <= day <= 31:
        return None
    for offset in range(13):
        month_index = today.month - 1 + offset
        candidate = _ordinal_safe_date(
            today.year + month_index // 12, month_index % 12 + 1, day
        )
        if candidate is not None and candidate >= today:
            return candidate
    return None  # pragma: no cover - a 1..31 day always lands within a year
