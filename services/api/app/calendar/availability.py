"""Pure availability engine for the calendar feature (Epic 11, story 11.05).

The correctness core: ``compute_availability`` decides whether a requested
start time is bookable, given the operator's busy intervals and a service's
rules. It is **pure** — no I/O, no ambient clock. ``now`` and
``requested_start`` are injected tz-aware datetimes (project-context rule:
never call ``datetime.now()`` inside logic), so every time-edge branch is
deterministically test-reachable.

Timezone discipline: working-hours windows are wall-clock strings interpreted
in ``project_tz`` per concrete date (so DST spring-forward / fall-back resolve
to the right absolute instants); all interval math is done in UTC.

Reasons a request is *not* available, checked in a stable order:
``in_past`` → ``outside_lookahead`` → ``date_exception`` (explicit closed date
or a resolved public holiday for the project country) → ``wrong_service_day``
→ ``outside_working_hours`` → ``busy``. A request is **available** iff it
survives every check.

The function consumes the canonical ``BusyInterval`` (story 11.04) and the
parsed ``AvailabilityServiceRule`` produced by ``parse_service_rule`` from the
``calendar_service_rules`` row dataclass (story 11.01) — one canonical DB shape,
one parsed shape for the engine. No raw JSON is interpreted inside the engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import holidays as _holidays_lib

from .calendar_client import BusyInterval
from .settings_repository import ServiceRule

# Reasons. Kept as module constants so callers/tests reference one source.
REASON_BUSY = "busy"
REASON_OUTSIDE_WORKING_HOURS = "outside_working_hours"
REASON_WRONG_SERVICE_DAY = "wrong_service_day"
REASON_DATE_EXCEPTION = "date_exception"
REASON_IN_PAST = "in_past"
REASON_OUTSIDE_LOOKAHEAD = "outside_lookahead"

# Weekday tokens accepted in working_hours / service_days JSON, mapped to the
# Python ``date.weekday()`` index (Monday == 0).
_WEEKDAY_INDEX: dict[str, int] = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

_DEFAULT_DURATION_MINUTES = 60
_DEFAULT_LOOKAHEAD_DAYS = 60


@dataclass(frozen=True)
class WorkingWindow:
    """A single ``[start, end)`` wall-clock window for one weekday."""

    start: time
    end: time


@dataclass(frozen=True)
class AvailabilityServiceRule:
    """Parsed, typed service rule the engine reasons over.

    Distinct from the loosely-typed DB-row ``ServiceRule`` (story 11.01): this
    is the canonical *engine* shape. ``working_hours`` maps a weekday index to
    one-or-more windows (two windows model a lunch gap). ``service_days`` is the
    set of weekday indices on which the service runs. ``date_exceptions`` are
    explicit closed dates. ``country_code`` drives public-holiday closures.
    """

    duration_minutes: int
    lookahead_days: int
    working_hours: dict[int, tuple[WorkingWindow, ...]] = field(default_factory=dict)
    service_days: frozenset[int] = field(default_factory=frozenset)
    date_exceptions: frozenset[date] = field(default_factory=frozenset)
    country_code: str = "RU"


@dataclass(frozen=True)
class AvailabilityResult:
    """Outcome of an availability check.

    ``available`` is the verdict; ``reason`` is ``None`` when available and one
    of the ``REASON_*`` constants otherwise.
    """

    available: bool
    reason: str | None = None

    @classmethod
    def available_result(cls) -> AvailabilityResult:
        return cls(available=True, reason=None)

    @classmethod
    def not_available(cls, reason: str) -> AvailabilityResult:
        return cls(available=False, reason=reason)


def _parse_time(value: str) -> time:
    """Parse a ``HH:MM`` (or ``HH:MM:SS``) wall-clock string into ``time``."""
    return time.fromisoformat(value)


def _parse_windows(raw: object) -> tuple[WorkingWindow, ...]:
    """Parse one weekday's working-hours value into ordered windows.

    Accepts a flat pair ``["09:00", "18:00"]`` (single window) or a list of
    pairs ``[["09:00", "13:00"], ["14:00", "18:00"]]`` (lunch gap).
    """
    if not raw:
        return ()
    # A flat ``[start, end]`` pair: first element is a string.
    if isinstance(raw[0], str):
        start, end = raw
        return (WorkingWindow(start=_parse_time(start), end=_parse_time(end)),)
    windows: list[WorkingWindow] = []
    for pair in raw:
        start, end = pair
        windows.append(WorkingWindow(start=_parse_time(start), end=_parse_time(end)))
    return tuple(windows)


def parse_service_rule(
    rule: ServiceRule,
    *,
    lookahead_days: int = _DEFAULT_LOOKAHEAD_DAYS,
    country_code: str = "RU",
) -> AvailabilityServiceRule:
    """Convert a DB-row :class:`ServiceRule` (story 11.01) into the engine shape.

    Unknown weekday tokens are ignored. ``duration_minutes`` falls back to a
    sane default when the row leaves it ``None``. ``lookahead_days`` and
    ``country_code`` come from project settings / runtime config (the engine
    needs both but the per-rule row carries neither).
    """
    working_hours: dict[int, tuple[WorkingWindow, ...]] = {}
    for token, raw in (rule.working_hours or {}).items():
        index = _WEEKDAY_INDEX.get(token.lower())
        if index is None:
            continue
        windows = _parse_windows(raw)
        if windows:
            working_hours[index] = windows

    service_days: set[int] = set()
    for token in rule.service_days or []:
        index = _WEEKDAY_INDEX.get(token.lower())
        if index is not None:
            service_days.add(index)

    date_exceptions: set[date] = set()
    for value in rule.date_exceptions or []:
        date_exceptions.add(date.fromisoformat(value))

    duration = rule.duration_minutes or _DEFAULT_DURATION_MINUTES
    return AvailabilityServiceRule(
        duration_minutes=duration,
        lookahead_days=lookahead_days,
        working_hours=working_hours,
        service_days=frozenset(service_days),
        date_exceptions=frozenset(date_exceptions),
        country_code=country_code,
    )


def _is_holiday(local_date: date, *, country_code: str) -> bool:
    """Resolve a project-country public holiday the same way the repo does."""
    try:
        calendar = _holidays_lib.country_holidays(country_code, years=local_date.year)
    except (NotImplementedError, KeyError):
        return False
    return local_date in calendar


def _window_bounds_utc(
    local_date: date, window: WorkingWindow, *, project_tz: ZoneInfo
) -> tuple[datetime, datetime]:
    """Resolve a wall-clock window on a date to absolute UTC instants.

    Building the aware datetime with ``project_tz`` lets ``zoneinfo`` pick the
    correct offset for that date, so spring-forward / fall-back days resolve to
    the right instants before we normalise to UTC for comparison.
    """
    start_local = datetime.combine(local_date, window.start, tzinfo=project_tz)
    end_local = datetime.combine(local_date, window.end, tzinfo=project_tz)
    return start_local.astimezone(_UTC), end_local.astimezone(_UTC)


_UTC = ZoneInfo("UTC")


def _overlaps(
    block_start: datetime, block_end: datetime, busy: BusyInterval
) -> bool:
    """Half-open interval overlap, in UTC."""
    busy_start = busy.start.astimezone(_UTC)
    busy_end = busy.end.astimezone(_UTC)
    return block_start < busy_end and busy_start < block_end


def compute_availability(
    *,
    now: datetime,
    requested_start: datetime,
    busy: tuple[BusyInterval, ...],
    service_rule: AvailabilityServiceRule,
    project_tz: ZoneInfo,
) -> AvailabilityResult:
    """Decide whether ``requested_start`` is available. Pure and total.

    The block under test is ``[requested_start, requested_start + duration)``.
    It is available iff it is (a) not in the past, (b) within the look-ahead
    horizon, (c) on a configured service-day that is neither an explicit
    date-exception nor a public holiday, (d) entirely inside one working-hours
    window for that weekday, and (e) free of every ``busy`` interval. All
    comparisons are done in UTC; weekday / date classification uses the local
    ``project_tz`` date of the requested start.
    """
    block_start = requested_start.astimezone(_UTC)
    duration = timedelta(minutes=service_rule.duration_minutes)
    block_end = block_start + duration
    now_utc = now.astimezone(_UTC)

    # (a) Not in the past.
    if block_start < now_utc:
        return AvailabilityResult.not_available(REASON_IN_PAST)

    # (b) Within the look-ahead horizon (measured from ``now``).
    horizon = now_utc + timedelta(days=service_rule.lookahead_days)
    if block_start >= horizon:
        return AvailabilityResult.not_available(REASON_OUTSIDE_LOOKAHEAD)

    local_start = requested_start.astimezone(project_tz)
    local_date = local_start.date()
    weekday = local_date.weekday()

    # (c) Closed dates: explicit exception or a resolved public holiday.
    if local_date in service_rule.date_exceptions or _is_holiday(
        local_date, country_code=service_rule.country_code
    ):
        return AvailabilityResult.not_available(REASON_DATE_EXCEPTION)

    # (c continued) Must be a configured service-day.
    if weekday not in service_rule.service_days:
        return AvailabilityResult.not_available(REASON_WRONG_SERVICE_DAY)

    # (d) The whole block must fit inside ONE working-hours window.
    windows = service_rule.working_hours.get(weekday, ())
    if not _fits_in_a_window(
        block_start, block_end, local_date, windows, project_tz=project_tz
    ):
        return AvailabilityResult.not_available(REASON_OUTSIDE_WORKING_HOURS)

    # (e) Free of every busy interval.
    for interval in busy:
        if _overlaps(block_start, block_end, interval):
            return AvailabilityResult.not_available(REASON_BUSY)

    return AvailabilityResult.available_result()


def find_earliest_slot(
    *,
    now: datetime,
    window: tuple[date, date],
    busy: tuple[BusyInterval, ...],
    service_rule: AvailabilityServiceRule,
    project_tz: ZoneInfo,
) -> datetime | None:
    """First available start instant within ``window`` (inclusive), or ``None``.

    Pure scan that reuses :func:`compute_availability` for every correctness
    check (past / look-ahead / closed-date / service-day / working-hours /
    busy), so the two stay in lock-step. Candidate starts are spaced by the
    service duration across each day's working windows; the first that
    ``compute_availability`` accepts wins.
    """
    duration = timedelta(minutes=service_rule.duration_minutes)
    day = window[0]
    while day <= window[1]:
        windows = service_rule.working_hours.get(day.weekday(), ())
        for win in windows:
            candidate = datetime.combine(day, win.start, tzinfo=project_tz)
            window_end = datetime.combine(day, win.end, tzinfo=project_tz)
            while candidate + duration <= window_end:
                result = compute_availability(
                    now=now,
                    requested_start=candidate,
                    busy=busy,
                    service_rule=service_rule,
                    project_tz=project_tz,
                )
                if result.available:
                    return candidate
                candidate += duration
        day += timedelta(days=1)
    return None


def _fits_in_a_window(
    block_start: datetime,
    block_end: datetime,
    local_date: date,
    windows: tuple[WorkingWindow, ...],
    *,
    project_tz: ZoneInfo,
) -> bool:
    """True iff ``[block_start, block_end)`` lies inside one working window."""
    for window in windows:
        window_start, window_end = _window_bounds_utc(
            local_date, window, project_tz=project_tz
        )
        if window_start <= block_start and block_end <= window_end:
            return True
    return False
