"""Tests for ``check_requested_availability`` — the shared "is THIS exact
slot free?" helper used by the sales completion handler.

Mirrors the orchestration in ``CalendarAvailabilityAnswerer._compute_answer``
but for a caller-resolved ``requested_start`` + ``service_rule``, returning a
small status object instead of customer-facing text.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from services.api.app.calendar.access_token_cache import CalendarReconnectNeeded
from services.api.app.calendar.calendar_client import (
    BusyInterval,
    CalendarProviderError,
    FreeBusy,
)
from services.api.app.calendar.requested_time_check import (
    RequestedAvailability,
    check_requested_availability,
)
from services.api.app.calendar.settings_repository import ServiceRule
from services.api.app.calendar.token_repository import TokenNotFound

_TZ = ZoneInfo("Europe/Moscow")
_NOW = datetime(2026, 5, 29, 9, 0, tzinfo=UTC)


def _rule() -> ServiceRule:
    week = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    return ServiceRule(
        id=1,
        project_id=1,
        name="Багги",
        duration_minutes=60,
        working_hours={day: [["09:00", "20:00"]] for day in week},
        service_days=week,
        date_exceptions=[],
        updated_at=None,
    )


class _TokenProvider:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self._raises = raises

    async def get_access_token(
        self, project_id, operator, *, operator_chat_id, trace_id
    ) -> str:
        if self._raises is not None:
            raise self._raises
        return "tok"


class _FreeBusy:
    def __init__(self, *, busy: tuple[BusyInterval, ...] = ()) -> None:
        self._busy = busy

    async def query_busy(
        self, *, access_token, time_min, time_max, trace_id, calendar_id="primary"
    ) -> FreeBusy:
        return FreeBusy(calendar_id="primary", busy=self._busy)


def _requested_tomorrow_14() -> datetime:
    # 30 May 2026, 14:00 Moscow time.
    return datetime(2026, 5, 30, 14, 0, tzinfo=_TZ)


async def _check(*, token_provider, freebusy, operator="@op", operator_chat_id=42):
    return await check_requested_availability(
        project_id=1,
        requested_start=_requested_tomorrow_14(),
        operator=operator,
        operator_chat_id=operator_chat_id,
        service_rule=_rule(),
        token_provider=token_provider,
        freebusy_client=freebusy,
        now=_NOW,
        project_tz=_TZ,
        lookahead_days=60,
        country_code="RU",
        trace_id="t-1",
    )


@pytest.mark.asyncio
async def test_free_slot_is_available() -> None:
    result = await _check(token_provider=_TokenProvider(), freebusy=_FreeBusy())
    assert result == RequestedAvailability(status="available", reason=None)


@pytest.mark.asyncio
async def test_busy_slot_is_unavailable() -> None:
    busy = (
        BusyInterval(
            start=datetime(2026, 5, 30, 13, 30, tzinfo=_TZ),
            end=datetime(2026, 5, 30, 15, 0, tzinfo=_TZ),
        ),
    )
    result = await _check(
        token_provider=_TokenProvider(), freebusy=_FreeBusy(busy=busy)
    )
    assert result.status == "unavailable"
    assert result.reason == "busy"
    # Nearest free slot that day (working hours start at 09:00, 14:00 is busy).
    assert result.alternative == datetime(2026, 5, 30, 9, 0, tzinfo=_TZ)


@pytest.mark.asyncio
async def test_missing_provider_is_not_connected() -> None:
    result = await _check(token_provider=None, freebusy=_FreeBusy())
    assert result.status == "not_connected"


@pytest.mark.asyncio
async def test_missing_freebusy_is_not_connected() -> None:
    result = await _check(token_provider=_TokenProvider(), freebusy=None)
    assert result.status == "not_connected"


@pytest.mark.asyncio
async def test_missing_operator_is_not_connected() -> None:
    result = await _check(
        token_provider=_TokenProvider(), freebusy=_FreeBusy(), operator=None
    )
    assert result.status == "not_connected"


@pytest.mark.asyncio
async def test_missing_operator_chat_id_is_not_connected() -> None:
    result = await _check(
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(),
        operator_chat_id=None,
    )
    assert result.status == "not_connected"


@pytest.mark.asyncio
async def test_reconnect_needed_is_error() -> None:
    result = await _check(
        token_provider=_TokenProvider(raises=CalendarReconnectNeeded()),
        freebusy=_FreeBusy(),
    )
    assert result.status == "error"
    assert result.reason == "reconnect_needed"


@pytest.mark.asyncio
async def test_token_not_found_is_error() -> None:
    result = await _check(
        token_provider=_TokenProvider(raises=TokenNotFound()),
        freebusy=_FreeBusy(),
    )
    assert result.status == "error"
    assert result.reason == "token_not_found"


@pytest.mark.asyncio
async def test_provider_error_is_error() -> None:
    result = await _check(
        token_provider=_TokenProvider(raises=CalendarProviderError("boom")),
        freebusy=_FreeBusy(),
    )
    assert result.status == "error"
    assert result.reason == "provider_error"


# --- Story 12.71 (round-17 R17-4): a specifically-named future date beyond the
# generic lookahead is still verified against the calendar (the lookahead bounds
# the "find me a slot" scan, not an explicit "is THIS date free?" check). ------


class _RecordingFreeBusy:
    """Captures the query window so we can assert it covers the requested date."""

    def __init__(self, *, busy: tuple[BusyInterval, ...] = ()) -> None:
        self._busy = busy
        self.time_min: datetime | None = None
        self.time_max: datetime | None = None

    async def query_busy(
        self, *, access_token, time_min, time_max, trace_id, calendar_id="primary"
    ) -> FreeBusy:
        self.time_min = time_min
        self.time_max = time_max
        return FreeBusy(calendar_id="primary", busy=self._busy)


async def _check_at(requested_start, *, freebusy, lookahead_days=60):
    return await check_requested_availability(
        project_id=1,
        requested_start=requested_start,
        operator="@op",
        operator_chat_id=42,
        service_rule=_rule(),
        token_provider=_TokenProvider(),
        freebusy_client=freebusy,
        now=_NOW,
        project_tz=_TZ,
        lookahead_days=lookahead_days,
        country_code="RU",
        trace_id="t-1",
    )


@pytest.mark.asyncio
async def test_far_future_free_date_is_available() -> None:
    # 15 Dec 2026 is ~200 days past _NOW (29 May) — beyond the 60-day lookahead,
    # but with no events it must read FREE, not a false "busy".
    requested = datetime(2026, 12, 15, 14, 0, tzinfo=_TZ)
    fb = _RecordingFreeBusy()
    result = await _check_at(requested, freebusy=fb)
    assert result == RequestedAvailability(status="available", reason=None)
    # The freebusy window was extended to actually cover the requested instant.
    assert fb.time_max is not None and fb.time_max >= requested


@pytest.mark.asyncio
async def test_far_future_busy_date_offers_same_day_alternative() -> None:
    requested = datetime(2026, 12, 15, 14, 0, tzinfo=_TZ)
    busy = (
        BusyInterval(
            start=datetime(2026, 12, 15, 13, 30, tzinfo=_TZ),
            end=datetime(2026, 12, 15, 15, 0, tzinfo=_TZ),
        ),
    )
    result = await _check_at(requested, freebusy=_FreeBusy(busy=busy))
    assert result.status == "unavailable"
    assert result.reason == "busy"
    assert result.alternative == datetime(2026, 12, 15, 9, 0, tzinfo=_TZ)


@pytest.mark.asyncio
async def test_beyond_safety_cap_stays_outside_lookahead() -> None:
    # A date past the ~400-day safety cap is still declined as outside the
    # bookable horizon (reason outside_lookahead) — never a false "busy".
    requested = datetime(2027, 12, 15, 14, 0, tzinfo=_TZ)  # ~1.5 years out
    result = await _check_at(requested, freebusy=_FreeBusy())
    assert result.status == "unavailable"
    assert result.reason == "outside_lookahead"
    assert result.alternative is None
