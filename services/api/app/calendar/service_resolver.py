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

# Absolute calendar dates: "1 июня", "2 июня", "15 сентября". The scoping LLM
# frequently *resolves* a relative reference ("в понедельник", even "завтра")
# into an absolute date before it reaches us, so the busy check must accept
# this shape too — otherwise an absolute date silently parses to ``None`` and
# the slot is handed off WITHOUT a calendar check. Month lookup is by prefix
# (one prefix covers every grammatical case: "июн" → "июня"/"июне"/"июнь").
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
_ABS_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+([а-яё]+)",
    re.IGNORECASE | re.UNICODE,
)


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
    return None


def _month_from_token(token: str) -> int | None:
    lowered = token.lower()
    for prefix, month in _MONTH_PREFIXES:
        if lowered.startswith(prefix):
            return month
    return None


def _extract_absolute_date(text: str, today: date) -> date | None:
    """Resolve an explicit ``"<day> <month>"`` date, or ``None``.

    Conservative, like the rest of the extractor: only commits when a day
    number pairs with a recognisable Russian month word and forms a valid
    date. The year defaults to ``today.year`` but rolls to the next year when
    the bare day/month has already passed locally — a customer naming "5 января"
    in December means the upcoming January, not a date eight months gone.
    """
    for match in _ABS_DATE_RE.finditer(text):
        month = _month_from_token(match.group(2))
        if month is None:
            continue
        day = int(match.group(1))
        candidate = _safe_date(year=today.year, month=month, day=day)
        if candidate is None:
            continue
        if candidate < today:
            candidate = _safe_date(year=today.year + 1, month=month, day=day)
        return candidate
    return None


def _safe_date(*, year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
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

    Returns a tz-aware ``datetime`` in ``project_tz`` only when BOTH a day
    anchor and a concrete clock time are present and in range; otherwise
    ``None``. A day anchor is a relative word ("сегодня"/"завтра"/
    "послезавтра"), a named weekday, OR an explicit "<day> <month>" calendar
    date ("2 июня") — the last because the scoping LLM often resolves a
    relative reference to an absolute date before it reaches us, and that
    resolved date must still reach the availability check. Still intentionally
    narrow: bare times without a day and fuzzy offsets like "через час" return
    ``None`` so the caller asks or escalates rather than guessing.

    Lemmatization reuses the shared :class:`RussianNormalizer` singleton (no
    parallel tokenizer); the clock is matched on the raw text since the
    lemmatizer drops the ``:`` separator.
    """
    clock = _extract_clock(text)
    if clock is None:
        return None

    local_now = now.astimezone(project_tz)
    lemmas = get_russian_normalizer().lemmas(text)
    offset = _extract_day_offset(lemmas, local_now.weekday())
    if offset is not None:
        target_date = (local_now + timedelta(days=offset)).date()
    else:
        # No relative/weekday anchor — accept an explicit "<day> <month>" date
        # so an LLM-resolved absolute date still reaches the calendar check.
        target_date = _extract_absolute_date(text.lower(), local_now.date())
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
