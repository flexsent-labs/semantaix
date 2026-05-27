"""``ProactiveFollowupJob`` reschedules rows inside quiet hours (21:00–10:00)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from services.scheduler.app.jobs.proactive_followup import (
    ProactiveFollowupJob,
    in_quiet_hours,
    next_morning_local,
)

_MSK = ZoneInfo("Europe/Moscow")


class _FakeApi:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.fire = AsyncMock()
        self.skip_stale = AsyncMock()
        self.reschedule = AsyncMock()

    async def list_due_followups(
        self, *, now: datetime
    ) -> list[dict[str, Any]]:
        return self.rows


def _row(**overrides: Any) -> dict[str, Any]:
    return {
        "id": 7,
        "chat_id": 42,
        "project_id": 1,
        "fire_at": datetime(2026, 5, 26, 0, 0, tzinfo=UTC).isoformat(),
        "status": "scheduled",
        "reason": None,
        "intent_dates": None,
        **overrides,
    }


def _at_local(hour: int, minute: int = 0) -> datetime:
    local = datetime(2026, 5, 26, hour, minute, tzinfo=_MSK)
    return local.astimezone(UTC)


@pytest.mark.asyncio
async def test_reschedules_at_22_to_next_10() -> None:
    now_utc = _at_local(22, 0)
    api = _FakeApi([_row()])
    job = ProactiveFollowupJob(
        api_client=api,
        clock=lambda: now_utc,
        project_tz_lookup=lambda _pid: "Europe/Moscow",
    )
    result = await job.run()
    assert result.rescheduled == 1
    assert result.fired == 0
    api.reschedule.assert_awaited_once()
    kwargs = api.reschedule.await_args.kwargs
    new_fire_at: datetime = kwargs["new_fire_at"]
    # 22:00 MSK → next 10:00 MSK is tomorrow.
    expected_local = datetime(2026, 5, 27, 10, 0, tzinfo=_MSK)
    assert new_fire_at.astimezone(_MSK) == expected_local


@pytest.mark.asyncio
async def test_reschedules_at_09_to_same_day_10() -> None:
    now_utc = _at_local(9, 0)
    api = _FakeApi([_row()])
    job = ProactiveFollowupJob(
        api_client=api,
        clock=lambda: now_utc,
        project_tz_lookup=lambda _pid: "Europe/Moscow",
    )
    result = await job.run()
    assert result.rescheduled == 1
    new_fire_at: datetime = api.reschedule.await_args.kwargs["new_fire_at"]
    expected = datetime(2026, 5, 26, 10, 0, tzinfo=_MSK)
    assert new_fire_at.astimezone(_MSK) == expected


@pytest.mark.asyncio
async def test_at_10_01_fires() -> None:
    now_utc = _at_local(10, 1)
    api = _FakeApi([_row()])
    job = ProactiveFollowupJob(
        api_client=api,
        clock=lambda: now_utc,
        project_tz_lookup=lambda _pid: "Europe/Moscow",
    )
    api.fire = AsyncMock(return_value={"ok": True})
    result = await job.run()
    assert result.fired == 1
    api.reschedule.assert_not_awaited()


def test_in_quiet_hours_window() -> None:
    msk = ZoneInfo("Europe/Moscow")
    assert in_quiet_hours(datetime(2026, 5, 26, 21, 0, tzinfo=msk)) is True
    assert in_quiet_hours(datetime(2026, 5, 26, 23, 59, tzinfo=msk)) is True
    assert in_quiet_hours(datetime(2026, 5, 27, 0, 0, tzinfo=msk)) is True
    assert in_quiet_hours(datetime(2026, 5, 27, 9, 59, tzinfo=msk)) is True
    assert in_quiet_hours(datetime(2026, 5, 27, 10, 0, tzinfo=msk)) is False
    assert in_quiet_hours(datetime(2026, 5, 27, 10, 1, tzinfo=msk)) is False
    assert in_quiet_hours(datetime(2026, 5, 27, 20, 59, tzinfo=msk)) is False


def test_next_morning_returns_today_when_before_10() -> None:
    msk = ZoneInfo("Europe/Moscow")
    nowm = datetime(2026, 5, 26, 9, 0, tzinfo=msk)
    assert next_morning_local(nowm) == datetime(2026, 5, 26, 10, 0, tzinfo=msk)


def test_next_morning_returns_tomorrow_when_after_10() -> None:
    msk = ZoneInfo("Europe/Moscow")
    nowm = datetime(2026, 5, 26, 22, 0, tzinfo=msk)
    assert next_morning_local(nowm) == datetime(2026, 5, 27, 10, 0, tzinfo=msk)
