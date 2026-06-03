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

# Story 12.50 (round-11 R11-2) — English day anchors. The Russian normalizer
# can't lemmatize English, so these are matched on the raw lowercased text by
# word boundary. "day after tomorrow" is checked before "tomorrow" (substring).
_EN_RELATIVE_DAYS: dict[str, int] = {"today": 0, "tomorrow": 1}
_EN_WEEKDAYS: dict[str, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

# "в 15:00" / "в 15.00" — explicit hour:minute.
_HH_MM = re.compile(r"\b(\d{1,2})[:.](\d{2})\b")
# "в 3 часа" / "в 15 часов" / "в 9 час" — hour + час-stem, minute defaults to 0.
_HH_CLOCK = re.compile(
    r"\b(\d{1,2})\s*час(?:а|ов|у)?\b",
    re.IGNORECASE | re.UNICODE,
)
# Story 12.50 (round-11 R11-2) — English am/pm clock: "2pm", "10am", "2:30pm",
# "2 pm". Checked BEFORE _HH_MM so "2:30pm" reads as 14:30, not 02:30.
_AMPM_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b", re.IGNORECASE)

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

# Story 12.50 (round-11 R11-2) — English "<month> <day>" / "<day> <month>"
# ("June 7", "7th of June"). Month lookup is by prefix like the Russian path,
# so "jun"/"june" both resolve; an optional ordinal suffix and "of" are
# tolerated. The captured word is validated against the prefix table, so a
# non-month word ("book 7") simply yields no month and is skipped.
_EN_MONTH_PREFIXES: tuple[tuple[str, int], ...] = (
    ("jan", 1),
    ("feb", 2),
    ("mar", 3),
    ("apr", 4),
    ("may", 5),
    ("jun", 6),
    ("jul", 7),
    ("aug", 8),
    ("sep", 9),
    ("oct", 10),
    ("nov", 11),
    ("dec", 12),
)
_EN_DATE_MD = re.compile(
    r"\b([a-z]{3,})\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.IGNORECASE
)
_EN_DATE_DM = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]{3,})\b", re.IGNORECASE
)

# Numeric / ISO / slash calendar dates (Story 12.38). The scoping LLM and
# customers sometimes emit these instead of Russian month words ("20.06 в 13:00",
# "01.06.2026", "2026-09-15", "20/06"). ISO and any year-bearing form are
# unambiguous; a bare dotted "DD.MM" collides with a "HH.MM" clock and is
# disambiguated in :func:`_extract_numeric_date`.
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_DOTTED_DATE_YEAR_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})\b")
_SLASH_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_DOTTED_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\b")


def _ampm_to_hm(match: re.Match[str]) -> tuple[int, int] | None:
    """Convert an ``_AMPM_RE`` match to 24h ``(hour, minute)``, or ``None`` if
    the 12-hour value is out of range (e.g. "14pm")."""
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if not (1 <= hour <= 12 and 0 <= minute <= 59):
        return None
    if match.group(3).lower() == "a":  # am: 12am → 00, else unchanged
        hour = 0 if hour == 12 else hour
    else:  # pm: 12pm → 12, else +12
        hour = 12 if hour == 12 else hour + 12
    return hour, minute


def _extract_clock(text: str) -> tuple[int, int] | None:
    """Return ``(hour, minute)`` if exactly one valid clock time is present.

    Conservative: an out-of-range value (hour > 23, minute > 59) yields
    ``None``; the answerer then clarifies instead of guessing.
    """
    ampm = _AMPM_RE.search(text)
    if ampm is not None:
        return _ampm_to_hm(ampm)
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


def extract_all_clocks(text: str) -> list[tuple[int, int]]:
    """Every distinct ``(hour, minute)`` clock time in ``text``, in order.

    Covers am/pm ("10am", "2:30pm"), explicit "HH:MM"/"HH.MM", and the Russian
    "N часов" form. Used by the pitching negotiation (Story 12.51, round-11
    R11-1) to tell a counter-offered time apart from the bot's own proposal
    when a follow-up names a bare time ("а давайте тогда в 12:00").
    """
    found: list[tuple[int, int]] = []
    chars = list(text)
    for match in _AMPM_RE.finditer(text):
        converted = _ampm_to_hm(match)
        if converted is not None:
            found.append(converted)
        for i in range(*match.span()):  # blank in place so HH:MM skips "2:30pm"
            chars[i] = " "
    work = "".join(chars)
    for match in _HH_MM.finditer(work):
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            found.append((hour, minute))
    for match in _HH_CLOCK.finditer(work):
        hour = int(match.group(1))
        if 0 <= hour <= 23:
            found.append((hour, 0))
    distinct: list[tuple[int, int]] = []
    for clock in found:
        if clock not in distinct:
            distinct.append(clock)
    return distinct


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


def _en_month_from_token(token: str) -> int | None:
    lowered = token.lower()
    for prefix, month in _EN_MONTH_PREFIXES:
        if lowered.startswith(prefix):
            return month
    return None


def _extract_en_absolute_date(text: str, today: date) -> date | None:
    """Resolve an English ``"<month> <day>"`` / ``"<day> <month>"`` date, or None.

    Mirrors :func:`_extract_absolute_date` (year defaults to today's, rolls to
    next year when the bare day/month already passed). Only commits when the
    word is a recognised English month, so non-month words are skipped.
    """
    for regex, day_group, month_group in (
        (_EN_DATE_MD, 2, 1),
        (_EN_DATE_DM, 1, 2),
    ):
        for match in regex.finditer(text):
            month = _en_month_from_token(match.group(month_group))
            if month is None:
                continue
            day = int(match.group(day_group))
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


def _resolve_dated(
    *, day: int, month: int, today: date, explicit_year: int | None
) -> date | None:
    """Build a date from day/month, inferring + rolling the year when omitted.

    An explicit year is honored verbatim (even if already past — the customer
    said so). An omitted year defaults to ``today``'s and rolls to next year
    when the bare day/month has already lapsed, matching the Russian-word path
    (:func:`_extract_absolute_date`) so "02.06" behaves like "2 июня".
    """
    if explicit_year is not None:
        year = explicit_year if explicit_year >= 100 else 2000 + explicit_year
        return _safe_date(year=year, month=month, day=day)
    candidate = _safe_date(year=today.year, month=month, day=day)
    if candidate is None:
        return None
    if candidate < today:
        candidate = _safe_date(year=today.year + 1, month=month, day=day)
    return candidate


def _extract_numeric_date(
    text: str, today: date
) -> tuple[date, tuple[int, int]] | None:
    """Locate a numeric/ISO/slash calendar date and its text span, or ``None``.

    Order matters: ISO and dotted/slash forms that carry a year are unambiguous
    and matched first. A bare dotted "DD.MM" collides with a "HH.MM" clock, so it
    is accepted as a date only when removing it still leaves a parseable clock
    (the booking shape "20.06 в 13:00") — otherwise it stays a time ("в 15.30").
    The span lets the caller strip the date before clock extraction so the clock
    regex never re-reads a dotted date as a time.
    """
    iso = _ISO_DATE_RE.search(text)
    if iso is not None:
        resolved = _safe_date(
            year=int(iso.group(1)), month=int(iso.group(2)), day=int(iso.group(3))
        )
        if resolved is not None:
            return resolved, iso.span()

    dotted_year = _DOTTED_DATE_YEAR_RE.search(text)
    if dotted_year is not None:
        resolved = _resolve_dated(
            day=int(dotted_year.group(1)),
            month=int(dotted_year.group(2)),
            today=today,
            explicit_year=int(dotted_year.group(3)),
        )
        if resolved is not None:
            return resolved, dotted_year.span()

    slash = _SLASH_DATE_RE.search(text)
    if slash is not None:
        year_str = slash.group(3)
        resolved = _resolve_dated(
            day=int(slash.group(1)),
            month=int(slash.group(2)),
            today=today,
            explicit_year=int(year_str) if year_str else None,
        )
        if resolved is not None:
            return resolved, slash.span()

    for dotted in _DOTTED_DATE_RE.finditer(text):
        resolved = _resolve_dated(
            day=int(dotted.group(1)),
            month=int(dotted.group(2)),
            today=today,
            explicit_year=None,
        )
        if resolved is None:
            continue
        remainder = text[: dotted.start()] + " " + text[dotted.end() :]
        if _extract_clock(remainder) is not None:
            return resolved, dotted.span()

    return None


def _strip_span(text: str, span: tuple[int, int]) -> str:
    """Return ``text`` with ``span`` blanked out (so tokens don't merge)."""
    start, end = span
    return text[:start] + " " + text[end:]


def _extract_day_offset(
    lemmas: list[str], text: str, today_weekday: int
) -> int | None:
    """Resolve a day anchor to a non-negative offset from today, or ``None``.

    Relative words win over weekday names. A named weekday resolves to its next
    occurrence (today counts only when it *is* that weekday). When neither a
    relative word nor a weekday is present, the day is ambiguous → ``None``.
    Russian is matched on lemmas; English (Story 12.50, R11-2) on the raw
    lowercased ``text`` by word boundary, since it can't be lemmatized.
    """
    for lemma in lemmas:
        if lemma in _RELATIVE_DAYS:
            return _RELATIVE_DAYS[lemma]
    for lemma in lemmas:
        target = _WEEKDAYS.get(lemma)
        if target is not None:
            return (target - today_weekday) % 7
    low = text.lower()
    if "day after tomorrow" in low:  # checked before "tomorrow" (substring)
        return 2
    for word, offset in _EN_RELATIVE_DAYS.items():
        if re.search(rf"\b{word}\b", low):
            return offset
    for word, index in _EN_WEEKDAYS.items():
        if re.search(rf"\b{word}\b", low):
            return (index - today_weekday) % 7
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
    local_now = now.astimezone(project_tz)
    lemmas = get_russian_normalizer().lemmas(text)
    offset = _extract_day_offset(lemmas, text, local_now.weekday())

    # Locate a numeric/ISO/slash date ("20.06", "01.06.2026", "2026-09-15",
    # "20/06") up front and remember its span. Stripping that span before the
    # clock extractor runs is what stops the _HH_MM regex from greedily reading a
    # dotted "DD.MM" date as a "HH.MM" time (Story 12.38, D10 #30).
    numeric = _extract_numeric_date(text, local_now.date())

    if offset is not None:
        # A relative/weekday anchor still wins, but a numeric date alongside it
        # must be stripped so the clock isn't read off the date digits.
        target_date = (local_now + timedelta(days=offset)).date()
        clock_source = _strip_span(text, numeric[1]) if numeric is not None else text
    elif numeric is not None:
        target_date = numeric[0]
        clock_source = _strip_span(text, numeric[1])
    else:
        # No relative/weekday/numeric anchor — accept an explicit "<day> <month>"
        # date so an LLM-resolved absolute date still reaches the calendar check.
        # Russian month words first, then English (Story 12.50, R11-2).
        target_date = _extract_absolute_date(
            text.lower(), local_now.date()
        ) or _extract_en_absolute_date(text.lower(), local_now.date())
        if target_date is None:
            return None
        clock_source = text

    clock = _extract_clock(clock_source)
    if clock is None:
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


def extract_requested_date(
    *,
    text: str,
    now: datetime,
    project_tz: ZoneInfo,
) -> date | None:
    """Best-effort parse of just the requested *date* (no clock), or ``None``.

    The date half of :func:`extract_requested_start` — relative word, weekday,
    numeric/ISO/slash, or "<day> <month>" (RU or EN). Used by the vague-time
    window flow (Story 12.60, round-14): "завтра во второй половине дня" carries
    a date but no concrete clock, so the bot checks that day's window instead of
    declining or asking blindly.
    """
    local_now = now.astimezone(project_tz)
    lemmas = get_russian_normalizer().lemmas(text)
    offset = _extract_day_offset(lemmas, text, local_now.weekday())
    if offset is not None:
        return (local_now + timedelta(days=offset)).date()
    numeric = _extract_numeric_date(text, local_now.date())
    if numeric is not None:
        return numeric[0]
    return _extract_absolute_date(
        text.lower(), local_now.date()
    ) or _extract_en_absolute_date(text.lower(), local_now.date())
