"""``ProactiveFollowupJob`` skips rows whose intent date already lapsed."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from services.scheduler.app.jobs.proactive_followup import ProactiveFollowupJob


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


_NOON_UTC = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)


def _row(intent_dates: str | None) -> dict[str, Any]:
    return {
        "id": 7,
        "chat_id": 42,
        "project_id": 1,
        "fire_at": _NOON_UTC.isoformat(),
        "status": "scheduled",
        "reason": None,
        "intent_dates": intent_dates,
    }


@pytest.mark.asyncio
async def test_skips_stale_when_intent_date_is_in_the_past() -> None:
    api = _FakeApi([_row("20 апреля")])
    job = ProactiveFollowupJob(
        api_client=api,
        clock=lambda: _NOON_UTC,
        project_tz_lookup=lambda _pid: "UTC",
    )
    result = await job.run()
    assert result.skipped_stale == 1
    assert result.fired == 0
    api.skip_stale.assert_awaited_once_with(7)
    api.fire.assert_not_awaited()
    api.reschedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_does_not_skip_when_intent_dates_is_none() -> None:
    api = _FakeApi([_row(None)])
    job = ProactiveFollowupJob(
        api_client=api,
        clock=lambda: _NOON_UTC,
        project_tz_lookup=lambda _pid: "UTC",
    )
    result = await job.run()
    assert result.skipped_stale == 0
    assert result.fired == 1


@pytest.mark.asyncio
async def test_does_not_skip_when_intent_date_is_today() -> None:
    # 2026-05-26 is _NOON_UTC.date(); strictly-before-today only.
    api = _FakeApi([_row("26 мая")])
    job = ProactiveFollowupJob(
        api_client=api,
        clock=lambda: _NOON_UTC,
        project_tz_lookup=lambda _pid: "UTC",
    )
    result = await job.run()
    assert result.skipped_stale == 0
    assert result.fired == 1
