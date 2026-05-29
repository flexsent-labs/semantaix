"""Unit tests for ``find_earliest_slot`` — first free start in a date window."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from services.api.app.calendar.availability import (
    find_earliest_slot,
    parse_service_rule,
)
from services.api.app.calendar.calendar_client import BusyInterval
from services.api.app.calendar.settings_repository import ServiceRule

_TZ = ZoneInfo("Europe/Moscow")
_NOW = datetime(2026, 5, 29, 6, 0, tzinfo=UTC)  # 09:00 Moscow, 29 May (Fri)


def _rule(*, days=None, hours="09:00", end="12:00", duration=60):
    week = days or ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    return parse_service_rule(
        ServiceRule(
            id=1,
            project_id=1,
            name="Багги",
            duration_minutes=duration,
            working_hours={day: [[hours, end]] for day in week},
            service_days=week,
            date_exceptions=[],
            updated_at=None,
        ),
        lookahead_days=60,
        country_code="RU",
    )


def test_returns_first_free_slot_when_all_free() -> None:
    slot = find_earliest_slot(
        now=_NOW,
        window=(date(2026, 5, 30), date(2026, 5, 30)),
        busy=(),
        service_rule=_rule(),
        project_tz=_TZ,
    )
    assert slot == datetime(2026, 5, 30, 9, 0, tzinfo=_TZ)


def test_skips_busy_to_next_step() -> None:
    busy = (
        BusyInterval(
            start=datetime(2026, 5, 30, 9, 0, tzinfo=_TZ),
            end=datetime(2026, 5, 30, 10, 0, tzinfo=_TZ),
        ),
    )
    slot = find_earliest_slot(
        now=_NOW,
        window=(date(2026, 5, 30), date(2026, 5, 30)),
        busy=busy,
        service_rule=_rule(),
        project_tz=_TZ,
    )
    assert slot == datetime(2026, 5, 30, 10, 0, tzinfo=_TZ)


def test_returns_none_when_day_fully_busy() -> None:
    busy = (
        BusyInterval(
            start=datetime(2026, 5, 30, 9, 0, tzinfo=_TZ),
            end=datetime(2026, 5, 30, 12, 0, tzinfo=_TZ),
        ),
    )
    slot = find_earliest_slot(
        now=_NOW,
        window=(date(2026, 5, 30), date(2026, 5, 30)),
        busy=busy,
        service_rule=_rule(),
        project_tz=_TZ,
    )
    assert slot is None


def test_rolls_to_next_service_day() -> None:
    # Only Sunday is a service day; window starts Saturday → first slot is Sun.
    slot = find_earliest_slot(
        now=_NOW,
        window=(date(2026, 5, 30), date(2026, 5, 31)),  # Sat, Sun
        busy=(),
        service_rule=_rule(days=["sun"]),
        project_tz=_TZ,
    )
    assert slot == datetime(2026, 5, 31, 9, 0, tzinfo=_TZ)


def test_returns_none_when_no_service_day_in_window() -> None:
    slot = find_earliest_slot(
        now=_NOW,
        window=(date(2026, 5, 30), date(2026, 5, 30)),  # Saturday
        busy=(),
        service_rule=_rule(days=["mon"]),
        project_tz=_TZ,
    )
    assert slot is None
