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

import calendar
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
# Negative offsets («вчера», «позавчера») resolve to past dates so the past-date
# guard (Story 12.73, round-18 R18-1) can reject them instead of silently
# handing off a booking into the past.
_RELATIVE_DAYS: dict[str, int] = {
    "позавчера": -2,
    "вчера": -1,
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
    # pymorphy3 lemmatizes «воскресенье»/«в воскресенье» → «воскресение»; match
    # that lemma too so Sunday is parsed (round-16 bug — Sunday never resolved).
    "воскресение": 6,
}

# A customer often answers a date question with one short, misspelled word
# (``завтро``, ``завтре``, ``субота``).  Keep this correction layer deliberately
# narrow: only relative days and weekdays are eligible, and the edit distance
# is bounded by the length of the temporal word.  This must not become a
# general-purpose spell checker that turns an unrelated message into a date.
_TEMPORAL_CANONICAL_WORDS: tuple[str, ...] = tuple(
    dict.fromkeys((*_RELATIVE_DAYS, *_WEEKDAYS))
)
_CYRILLIC_WORD_RE = re.compile(r"[а-яё]+", re.IGNORECASE | re.UNICODE)


def _temporal_typo_budget(word: str) -> int:
    return 2 if len(word) >= 9 else 1


def _temporal_edit_distance(left: str, right: str, *, limit: int) -> int:
    """Levenshtein distance with an early exit for the temporal typo budget."""
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, start=1):
        current = [row]
        for column, right_char in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (left_char != right_char),
                )
            )
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def normalize_temporal_typos(text: str) -> str:
    """Correct only conservative Russian day/date-anchor typos in ``text``.

    The returned text keeps its punctuation and spacing.  Exact words are
    unchanged; a fuzzy replacement is made only when there is one clear
    closest temporal word within its length-based edit budget.
    """
    if not text or not text.strip():
        return text

    def replace(match: re.Match[str]) -> str:
        token = match.group(0).lower()
        if token in _TEMPORAL_CANONICAL_WORDS:
            return token
        candidates: list[tuple[int, str]] = []
        for canonical in _TEMPORAL_CANONICAL_WORDS:
            # Do not collapse a longer, valid time-of-day word such as
            # ``вечера`` into the shorter relative day ``вчера``.
            if len(token) > len(canonical):
                continue
            limit = _temporal_typo_budget(canonical)
            distance = _temporal_edit_distance(token, canonical, limit=limit)
            if distance <= limit:
                candidates.append((distance, canonical))
        if not candidates:
            return token
        candidates.sort()
        best_distance, best_word = candidates[0]
        if len(candidates) > 1 and candidates[1][0] == best_distance:
            return token
        return best_word

    return _CYRILLIC_WORD_RE.sub(replace, text)

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
# Story 12.50 (round-11 R11-2) — English am/pm clock: "2pm", "10am", "2:30pm",
# "2 pm". Checked BEFORE _HH_MM so "2:30pm" reads as 14:30, not 02:30.
_AMPM_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b", re.IGNORECASE)

# Story 12.68/12.70 (round-17 R17-1/R17-3) — Russian cardinal number WORDS for
# clock hours ("в три часа дня" → 15:00) and relative offsets ("через два часа").
# Small map (1-12, with the gendered один/одна, два/две) — anything outside it is
# declined rather than guessed.
_WORD_NUMBERS: dict[str, int] = {
    "один": 1,
    "одна": 1,
    "одно": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
}
# Longest-first so the alternation prefers "двенадцать" over a "две" prefix.
_WORD_HOURS_ALT = "|".join(sorted(_WORD_NUMBERS, key=len, reverse=True))

# Story 12.68 (round-17 R17-1) — part-of-day qualifier that promotes a 1-12 hour
# to 24h: "утра"/"ночи" keep it (12 → 0), "дня"/"вечера" add 12 (12 stays 12).
_DAYPART = r"(?:утр[аоу]м?|дн[яёе]м?|вечер[ао]м?|ноч[ьи]ю?)"
# "в три часа дня" / "в 3 часа" / "в час дня" — an optional cardinal (digit or
# word; bare "час" means one o'clock) + час-stem + an optional part-of-day.
_CLOCK_CHAS_RE = re.compile(
    rf"\b(?:(?P<num>\d{{1,2}}|{_WORD_HOURS_ALT})\s*)?час(?:а|ов|у)?\b"
    rf"(?:\s+(?P<qual>{_DAYPART}))?",
    re.IGNORECASE | re.UNICODE,
)
# "в девять утра" / "в 9 утра" / "в семь вечера" — a cardinal directly followed
# by a part-of-day, with no "час" word in between.
_CLOCK_DAYPART_RE = re.compile(
    rf"\b(?P<num>\d{{1,2}}|{_WORD_HOURS_ALT})\s+(?P<qual>{_DAYPART})\b",
    re.IGNORECASE | re.UNICODE,
)
# "в полдень" → 12:00, "в полночь" → 00:00 (incl. the полу- genitive forms).
_NOON_MIDNIGHT_RE = re.compile(
    r"\b(?P<noon>полдень|полудень)\b"
    r"|\b(?P<mid>полночь|полуночь|полночи|полуночи)\b",
    re.IGNORECASE | re.UNICODE,
)

# Story 12.79 (round-19 R19-2) — half-form times: «пол<ordinal>» = 30 min before
# that hour («полвторого» → 1:30, «полпервого» → 12:30). Built after the ordinal
# maps below; the regex is defined there once those exist.

# Story 12.78 (round-19 R19-3) — an OPEN-ENDED time bound names a range, not a
# commitment: «после 15:00» / «не раньше 16:00» (lower), «до 14:00» / «не позже
# 18:00» (upper). The number must look like a clock (":MM" or "час…") so a date
# («до 6 июня») isn't mistaken for a bound.
_LOWER_BOUND_RE = re.compile(
    r"(?:после|не\s+раньше|начиная\s+с|где-то\s+после)\s+(\d{1,2})(?::\d{2}|\s*час\w*)",
    re.IGNORECASE | re.UNICODE,
)
_UPPER_BOUND_RE = re.compile(
    r"(?:до|не\s+позже|не\s+позднее)\s+(\d{1,2})(?::\d{2}|\s*час\w*)",
    re.IGNORECASE | re.UNICODE,
)


def extract_time_bound(text: str) -> tuple[str, int] | None:
    """An open-ended time bound as ``("after"|"before", hour)``, or ``None``.

    Story 12.78 (round-19 R19-3). Lets the caller treat «после 15:00» / «до
    14:00» as underspecified (clarify) rather than booking the bare hour, and
    lets the vague-window flow propose a slot inside the bound.
    """
    lower = _LOWER_BOUND_RE.search(text)
    if lower is not None and 0 <= int(lower.group(1)) <= 23:
        return ("after", int(lower.group(1)))
    upper = _UPPER_BOUND_RE.search(text)
    if upper is not None and 0 <= int(upper.group(1)) <= 23:
        return ("before", int(upper.group(1)))
    return None
# Story 12.70 (round-17 R17-3) — relative offset "через N <unit>" measured from
# "now": "через два часа", "через час", "через 30 минут", "через 3 дня",
# "через полчаса", "через неделю". The count may be a digit, a number word, or
# "пол" (half); a missing count means one.
_THROUGH_RE = re.compile(
    rf"\bчерез\s+(?:(?P<num>\d{{1,3}}|пол|{_WORD_HOURS_ALT})\s*)?"
    r"(?P<unit>час\w*|минут\w*|мин\b|недел\w*|нед\b|дн\w*|день|ден[ьёе]\w*)",
    re.IGNORECASE | re.UNICODE,
)

# Story 12.75 (round-18 R18-2) — «сейчас» / «прямо сейчас» → the current instant.
# «не сейчас» (not now) is a deferral, not a booking time, so it's excluded.
_NOW_RE = re.compile(r"\b(?:прямо\s+|прям\s+)?сейчас\b", re.IGNORECASE | re.UNICODE)
_NOT_NOW_RE = re.compile(r"\bне\s+сейчас\b", re.IGNORECASE | re.UNICODE)

# Story 12.76 (round-18 R18-3) — a no-colon compact time after «в»: «в 1400» →
# 14:00, «в 14 00» → 14:00, «в 900» → 09:00. The «в» prefix and the trailing
# no-more-digits guard keep it from eating counts/prices ("в 14000", "5 человек").
_COMPACT_HHMM_RE = re.compile(
    r"\bв\s+(\d{1,2})\s*(\d{2})(?!\d)", re.IGNORECASE | re.UNICODE
)

# Story 12.72 (round-18 R18-4) — Russian ordinal DAY words («девятого в 12:00» =
# the 9th). Genitive ordinals 1-31, including the compound «двадцать третьего».
_ORDINAL_UNITS: dict[str, int] = {
    "первого": 1,
    "второго": 2,
    "третьего": 3,
    "четвёртого": 4,
    "четвертого": 4,
    "пятого": 5,
    "шестого": 6,
    "седьмого": 7,
    "восьмого": 8,
    "девятого": 9,
}
_ORDINAL_SIMPLE: dict[str, int] = {
    **_ORDINAL_UNITS,
    "десятого": 10,
    "одиннадцатого": 11,
    "двенадцатого": 12,
    "тринадцатого": 13,
    "четырнадцатого": 14,
    "пятнадцатого": 15,
    "шестнадцатого": 16,
    "семнадцатого": 17,
    "восемнадцатого": 18,
    "девятнадцатого": 19,
    "двадцатого": 20,
    "тридцатого": 30,
}
_ORDINAL_TENS: dict[str, int] = {"двадцать": 20, "тридцать": 30}
_ORDINAL_UNITS_ALT = "|".join(sorted(_ORDINAL_UNITS, key=len, reverse=True))
_ORDINAL_SIMPLE_ALT = "|".join(sorted(_ORDINAL_SIMPLE, key=len, reverse=True))
_ORDINAL_COMPOUND_RE = re.compile(
    rf"\b(?P<tens>двадцать|тридцать)\s+(?P<unit>{_ORDINAL_UNITS_ALT})\b",
    re.IGNORECASE | re.UNICODE,
)
_ORDINAL_SIMPLE_RE = re.compile(
    rf"\b(?P<ord>{_ORDINAL_SIMPLE_ALT})\b", re.IGNORECASE | re.UNICODE
)
# Story 12.79 (round-19 R19-2) — half-form clock «пол<ordinal>» («полвторого»),
# with an optional part-of-day. Reuses the ordinal vocabulary above.
_HALF_FORM_RE = re.compile(
    rf"\bпол(?P<ord>{_ORDINAL_SIMPLE_ALT})(?:\s+(?P<qual>{_DAYPART}))?",
    re.IGNORECASE | re.UNICODE,
)
# Story 12.83 (round-20 R20-3) — «без <minutes> <hour>» («без пятнадцати три» =
# 14:45): N minutes before the named (upcoming) hour. Minutes are the common
# genitive forms incl. «четверти» (quarter = 15); the hour is a cardinal word or
# «час» (= 1). Longest-first so «двадцати пяти» beats «двадцати».
_BEFORE_MINUTES: dict[str, int] = {
    "пяти": 5,
    "десяти": 10,
    "пятнадцати": 15,
    "четверти": 15,
    "двадцати пяти": 25,
    "двадцати": 20,
}
_BEFORE_MINUTES_ALT = "|".join(sorted(_BEFORE_MINUTES, key=len, reverse=True))
_BEFORE_HOUR_ALT = "час\\w*|" + _WORD_HOURS_ALT
_BEFORE_RE = re.compile(
    rf"\bбез\s+(?P<min>{_BEFORE_MINUTES_ALT})\s+(?P<hour>{_BEFORE_HOUR_ALT})"
    rf"(?:\s+(?P<qual>{_DAYPART}))?",
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

# Story 12.20 — numeric ordinal day suffixes: «31-ое», «9-го», «3-его», «5-е».
# Matches digit(s) + hyphen + a Russian ordinal adjectival ending.  Lower priority
# than «<day> <month>» (which is more specific) and than ISO/dotted dates.
_NUMERIC_ORDINAL_DATE_RE = re.compile(
    r"\b(\d{1,2})-(?:ого|его|ое|ем|ому|е(?!\w)|го)\b",
    re.IGNORECASE | re.UNICODE,
)

# Story 12.20 — bare «в N» clock: «в 8», «в 15».  Fires only when the digit is
# NOT immediately followed by «:», «.», another digit, or an ordinal suffix
# (which would make it a date like «в 8-ое»).  Lower priority than all other
# clock patterns so «в 8 часов» and «в 8:00» still win.
_VN_BARE_HOUR_RE = re.compile(
    r"\bв\s+(\d{1,2})(?!\d|[-:.]|(?:ого|его|ое|ем|ому|е(?!\w)|го))\b",
    re.IGNORECASE | re.UNICODE,
)


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


def _apply_daypart(hour: int, qual: str | None) -> int | None:
    """Promote a 1-12 ``hour`` to 24h using a part-of-day qualifier, or ``None``
    when the result is out of range.

    "утра"/"ночи" (morning/night) keep the hour, mapping 12 → 0 (полночь side);
    "дня"/"вечера" (afternoon/evening) add 12, leaving 12 as noon.
    """
    if qual is not None:
        q = qual.lower()
        if q.startswith("утр") or q.startswith("ноч"):
            hour = 0 if hour == 12 else hour
        elif q.startswith("дн") or q.startswith("веч"):
            hour = hour if hour == 12 else hour + 12
    if 0 <= hour <= 23:
        return hour
    return None


def _scan_clocks(text: str) -> list[tuple[int, int, int, int]]:
    """Every valid clock time in ``text`` as ``(hour, minute, start, end)``.

    A single scanner shared by :func:`_extract_clock` and
    :func:`extract_all_clocks`. It recognises (in priority order, so that a
    higher-priority match wins any character it overlaps): am/pm, the noon/
    midnight words, the word-or-digit "N часов [дня]" form, the bare
    "N <part-of-day>" form, and explicit "HH:MM"/"HH.MM". The accepted matches
    are returned sorted by their position in the text.
    """
    groups: list[list[tuple[int, int, int, int]]] = []

    ampm: list[tuple[int, int, int, int]] = []
    for m in _AMPM_RE.finditer(text):
        hm = _ampm_to_hm(m)
        if hm is not None:
            ampm.append((m.start(), m.end(), hm[0], hm[1]))
    groups.append(ampm)

    noon: list[tuple[int, int, int, int]] = []
    for m in _NOON_MIDNIGHT_RE.finditer(text):
        hour = 12 if m.group("noon") else 0
        noon.append((m.start(), m.end(), hour, 0))
    groups.append(noon)

    # «без <minutes> <hour>» → N min before the named hour («без пятнадцати три»
    # → 14:45). Higher priority than the day-part group so «без … три ночи» isn't
    # mis-read as «три ночи».
    before: list[tuple[int, int, int, int]] = []
    for m in _BEFORE_RE.finditer(text):
        hword = m.group("hour").lower()
        hour_n = 1 if hword.startswith("час") else _WORD_NUMBERS[hword]
        hour = 12 if hour_n == 1 else hour_n - 1
        minute = 60 - _BEFORE_MINUTES[m.group("min").lower()]
        qual = m.group("qual")
        if qual is not None:
            promoted = _apply_daypart(hour, qual)
            if promoted is None:  # pragma: no cover - defensive; unreachable for 1-12
                continue
            hour = promoted
        elif 1 <= hour <= 7:  # bare → daytime default (round-20 R20-3)
            hour += 12
        before.append((m.start(), m.end(), hour, minute))
    groups.append(before)

    # Half-form «пол<ordinal>» → (ordinal-1):30; «полпервого» → 12:30. A day-part
    # pins AM/PM; a bare early hour (1-7) defaults to the daytime (PM) reading.
    half: list[tuple[int, int, int, int]] = []
    for m in _HALF_FORM_RE.finditer(text):
        ordinal = _ORDINAL_SIMPLE[m.group("ord").lower()]
        if not 1 <= ordinal <= 12:
            continue  # «полдвадцатого» etc. isn't an hour
        hour = 12 if ordinal == 1 else ordinal - 1
        qual = m.group("qual")
        if qual is not None:
            # hour is 1-12, so _apply_daypart never goes out of range here.
            promoted = _apply_daypart(hour, qual)
            if promoted is None:  # pragma: no cover - defensive; unreachable for 1-12
                continue
            hour = promoted
        elif 1 <= hour <= 7:  # bare → daytime default (round-19 R19-2)
            hour += 12
        half.append((m.start(), m.end(), hour, 30))
    groups.append(half)

    for regex in (_CLOCK_CHAS_RE, _CLOCK_DAYPART_RE):
        chas: list[tuple[int, int, int, int]] = []
        for m in regex.finditer(text):
            num = m.group("num")
            qual = m.group("qual")
            if num is None and qual is None:
                continue  # a bare "час" is too ambiguous to be a clock
            base = int(num) if num and num.isdigit() else (
                _WORD_NUMBERS[num.lower()] if num else 1
            )
            hour = _apply_daypart(base, qual)
            if hour is not None:
                chas.append((m.start(), m.end(), hour, 0))
        groups.append(chas)

    hhmm: list[tuple[int, int, int, int]] = []
    for m in _HH_MM.finditer(text):
        hour, minute = int(m.group(1)), int(m.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            hhmm.append((m.start(), m.end(), hour, minute))
    groups.append(hhmm)

    # Story 12.76 (round-18 R18-3) — no-colon compact "в HHMM" / "в HH MM".
    compact: list[tuple[int, int, int, int]] = []
    for m in _COMPACT_HHMM_RE.finditer(text):
        hour, minute = int(m.group(1)), int(m.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            compact.append((m.start(), m.end(), hour, minute))
    groups.append(compact)

    # Story 12.20 — bare «в N» hour (lowest priority so «в 8 часов»/«в 8:00» win).
    vn: list[tuple[int, int, int, int]] = []
    for m in _VN_BARE_HOUR_RE.finditer(text):
        hour = int(m.group(1))
        if 1 <= hour <= 23:
            vn.append((m.start(), m.end(), hour, 0))
    groups.append(vn)

    accepted: list[tuple[int, int, int, int]] = []
    for group in groups:  # priority order
        for cand in group:
            if any(
                not (cand[1] <= a[0] or cand[0] >= a[1]) for a in accepted
            ):
                continue  # overlaps an already-accepted, higher-priority match
            accepted.append(cand)
    accepted.sort(key=lambda c: (c[0], c[1]))
    return [(hour, minute, start, end) for (start, end, hour, minute) in accepted]


def _extract_clock(text: str) -> tuple[int, int] | None:
    """Return the ``(hour, minute)`` of the LAST valid clock in ``text``.

    Last-wins (Story 12.69, round-17 R17-2): when a message restates the time
    ("в 14:00… нет, лучше в 12:00") the corrected, later value is the one used.
    ``None`` when no valid clock is present, so the answerer clarifies instead
    of guessing.
    """
    clocks = _scan_clocks(text)
    if not clocks:
        return None
    hour, minute, _, _ = clocks[-1]
    return hour, minute


def extract_all_clocks(text: str) -> list[tuple[int, int]]:
    """Every distinct ``(hour, minute)`` clock time in ``text``, in order.

    Covers am/pm ("10am", "2:30pm"), explicit "HH:MM"/"HH.MM", the Russian
    "N часов" form, and word-form times ("три часа дня", "полдень"). Used by the
    pitching negotiation (Story 12.51, round-11 R11-1) to tell a counter-offered
    time apart from the bot's own proposal when a follow-up names a bare time.
    """
    distinct: list[tuple[int, int]] = []
    for hour, minute, _, _ in _scan_clocks(text):
        if (hour, minute) not in distinct:
            distinct.append((hour, minute))
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


def _ordinal_day(text: str) -> int | None:
    """Resolve a Russian ordinal day word to its 1-31 number, or ``None``.

    Compound forms («двадцать третьего») win over the bare unit so "двадцать
    третьего" reads 23, not 3.
    """
    compound = _ORDINAL_COMPOUND_RE.search(text)
    if compound is not None:
        return _ORDINAL_TENS[compound.group("tens").lower()] + _ORDINAL_UNITS[
            compound.group("unit").lower()
        ]
    simple = _ORDINAL_SIMPLE_RE.search(text)
    if simple is not None:
        return _ORDINAL_SIMPLE[simple.group("ord").lower()]
    return None


def _extract_ordinal_date(text: str, today: date) -> date | None:
    """Resolve an ordinal day word to the next occurrence of that day-of-month.

    Story 12.72 (round-18 R18-4): «девятого» → the next 9th (this month if it
    hasn't passed, else a following month). Scans up to ~13 months so day
    numbers absent from short months (e.g. «тридцать первого») still resolve.
    """
    day = _ordinal_day(text)
    if day is None:
        return None
    year, month = today.year, today.month
    for _ in range(13):
        candidate = _safe_date(year=year, month=month, day=day)
        if candidate is not None and candidate >= today:
            return candidate
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return None


def _extract_numeric_ordinal_date(text: str, today: date) -> date | None:
    """Resolve a numeric ordinal like «31-ое» / «9-го» to the next occurrence.

    Story 12.20: same semantics as :func:`_extract_ordinal_date` but for the
    digit+suffix form («31-ое», «9-го», «3-его») instead of word ordinals.
    """
    m = _NUMERIC_ORDINAL_DATE_RE.search(text)
    if m is None:
        return None
    day = int(m.group(1))
    if not 1 <= day <= 31:
        return None
    year, month = today.year, today.month
    for _ in range(13):
        candidate = _safe_date(year=year, month=month, day=day)
        if candidate is not None and candidate >= today:
            return candidate
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return None  # pragma: no cover - unreachable for day 1-31; line 690 blocks day>31


def names_invalid_date(text: str, *, now: datetime, project_tz: ZoneInfo) -> bool:
    """True when ``text`` names a calendar date that doesn't exist — a Russian
    "<day> <month>" or a "DD.MM" whose day exceeds that month's length (round-16
    R16-1: «31 июня», «30 февраля», «31.06»). A "HH.MM" clock is excluded (its
    second number isn't a valid 1-12 month). Leap-February uses ``now``'s year.
    Distinct from "no date given" — lets the caller clarify instead of handing
    off a booking on an impossible date.
    """
    year = now.astimezone(project_tz).year

    def _out_of_range(month: int, day: int) -> bool:
        if not (1 <= month <= 12) or day < 1:
            return False
        return day > calendar.monthrange(year, month)[1]

    for match in _ABS_DATE_RE.finditer(text.lower()):
        month = _month_from_token(match.group(2))
        if month is not None and _out_of_range(month, int(match.group(1))):
            return True
    for match in _DOTTED_DATE_RE.finditer(text):
        if _out_of_range(int(match.group(2)), int(match.group(1))):
            return True
    return False


def names_past_date(text: str, *, now: datetime, project_tz: ZoneInfo) -> bool:
    """True when ``text`` resolves to a whole day strictly before today.

    Story 12.73 (round-18 R18-1): «вчера» / «позавчера» (and any input that
    resolves to a past date) should be rejected with a clarify, not handed off
    as a valid booking. Past-time-*today* is left to the calendar's in-past
    verdict; this guards a past *day*. Numeric/absolute dates roll forward, so
    in practice only the explicit past-day words trigger this.
    """
    requested = extract_requested_date(text=text, now=now, project_tz=project_tz)
    if requested is None:
        return False
    return requested < now.astimezone(project_tz).date()


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


def _extract_relative_offset(text: str, local_now: datetime) -> datetime | None:
    """Resolve a "через N <unit>" offset against ``local_now``, or ``None``.

    Story 12.70 (round-17 R17-3). Hours/minutes give a full instant
    (``now + delta``); a day/week offset keeps ``now``'s time-of-day unless the
    text also names an explicit clock ("через 3 дня в 14:00"). "пол" means half
    (полчаса → 30 min, полдня → 12 h). An unrecognised count/unit → ``None``.

    Story 12.75 (round-18 R18-2): «сейчас» / «прямо сейчас» resolve to ``now``
    itself (the «не сейчас» deferral is excluded).
    """
    if _NOW_RE.search(text) and not _NOT_NOW_RE.search(text):
        return local_now
    m = _THROUGH_RE.search(text)
    if m is None:
        return None
    num_raw = m.group("num")
    unit = m.group("unit").lower()
    half = num_raw is not None and num_raw.lower() == "пол"
    if num_raw is None or half:
        count = 1
    elif num_raw.isdigit():
        count = int(num_raw)
    else:
        # The regex's number alternation is built from _WORD_NUMBERS, so a
        # non-digit, non-"пол" match is always a known word.
        count = _WORD_NUMBERS[num_raw.lower()]

    if unit.startswith("час"):
        return local_now + (
            timedelta(minutes=30) if half else timedelta(hours=count)
        )
    if unit.startswith("мин"):
        return local_now + timedelta(minutes=count)
    if unit.startswith("нед"):
        target = local_now + timedelta(weeks=count)
    elif unit.startswith("дн") or unit.startswith("ден"):
        target = local_now + (timedelta(hours=12) if half else timedelta(days=count))
    else:  # pragma: no cover - regex only yields the units handled above
        return None

    # Day/week offset: honour an explicit clock if one is also named, else keep
    # the current time-of-day.
    remainder = text[: m.start()] + " " + text[m.end() :]
    clock = _extract_clock(remainder)
    if clock is not None:
        return datetime(
            target.year,
            target.month,
            target.day,
            clock[0],
            clock[1],
            tzinfo=target.tzinfo,
        )
    return target


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
    text = normalize_temporal_typos(text)
    local_now = now.astimezone(project_tz)

    # Story 12.78 (round-19 R19-3) — an open-ended bound («после 15:00», «до
    # 14:00») names a range, not a concrete time; decline so the caller clarifies
    # (the vague-window flow proposes a slot inside the bound) instead of booking
    # the bare hour.
    if extract_time_bound(text) is not None:
        return None

    # Story 12.70 (round-17 R17-3) — a relative "через N <unit>" offset resolves
    # to a concrete instant up front; it carries both date and time, so it wins
    # over the day-anchor + clock path below.
    relative = _extract_relative_offset(text, local_now)
    if relative is not None:
        return relative

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
        # Russian month words first, then English (Story 12.50, R11-2), then an
        # ordinal day word ("девятого" → next 9th; Story 12.72, round-18 R18-4),
        # then a numeric ordinal ("31-ое", "9-го"; Story 12.20).
        target_date = (
            _extract_absolute_date(text.lower(), local_now.date())
            or _extract_en_absolute_date(text.lower(), local_now.date())
            or _extract_ordinal_date(text.lower(), local_now.date())
            or _extract_numeric_ordinal_date(text.lower(), local_now.date())
        )
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
    text = normalize_temporal_typos(text)
    local_now = now.astimezone(project_tz)
    lemmas = get_russian_normalizer().lemmas(text)
    offset = _extract_day_offset(lemmas, text, local_now.weekday())
    if offset is not None:
        return (local_now + timedelta(days=offset)).date()
    numeric = _extract_numeric_date(text, local_now.date())
    if numeric is not None:
        return numeric[0]
    return (
        _extract_absolute_date(text.lower(), local_now.date())
        or _extract_en_absolute_date(text.lower(), local_now.date())
        or _extract_ordinal_date(text.lower(), local_now.date())
        or _extract_numeric_ordinal_date(text.lower(), local_now.date())
    )
