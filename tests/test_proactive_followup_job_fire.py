"""``ProactiveFollowupJob`` fires due rows outside quiet hours."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from services.scheduler.app.jobs.proactive_followup import ProactiveFollowupJob


class _FakeApi:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.fire = AsyncMock(return_value={"ok": True, "sent": True})
        self.skip_stale = AsyncMock()
        self.reschedule = AsyncMock()

    async def list_due_followups(
        self, *, now: datetime
    ) -> list[dict[str, Any]]:
        return self.rows


_NOON_UTC = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)


def _row(**overrides: Any) -> dict[str, Any]:
    return {
        "id": 1,
        "chat_id": 42,
        "project_id": 1,
        "fire_at": _NOON_UTC.isoformat(),
        "status": "scheduled",
        "reason": None,
        "intent_dates": None,
        **overrides,
    }


@pytest.mark.asyncio
async def test_due_outside_quiet_hours_fires() -> None:
    api = _FakeApi([_row()])
    job = ProactiveFollowupJob(
        api_client=api,
        clock=lambda: _NOON_UTC,
        project_tz_lookup=lambda _pid: "UTC",
    )
    result = await job.run()
    assert result.fired == 1
    assert result.skipped_stale == 0
    assert result.rescheduled == 0
    api.fire.assert_awaited_once_with(1)
    api.skip_stale.assert_not_awaited()
    api.reschedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_future_intent_date_does_not_skip() -> None:
    api = _FakeApi([_row(intent_dates="1 июня")])
    job = ProactiveFollowupJob(
        api_client=api,
        clock=lambda: _NOON_UTC,
        project_tz_lookup=lambda _pid: "UTC",
    )
    result = await job.run()
    assert result.fired == 1
    assert result.skipped_stale == 0


@pytest.mark.asyncio
async def test_no_due_rows_is_a_no_op() -> None:
    api = _FakeApi([])
    job = ProactiveFollowupJob(
        api_client=api,
        clock=lambda: _NOON_UTC,
        project_tz_lookup=lambda _pid: "UTC",
    )
    result = await job.run()
    assert result.total_processed() == 0
    api.fire.assert_not_awaited()


@pytest.mark.asyncio
async def test_due_call_failure_is_logged_and_returned() -> None:
    class _Api(_FakeApi):
        async def list_due_followups(
            self, *, now: datetime
        ) -> list[dict[str, Any]]:
            raise RuntimeError("boom")

    api = _Api([])
    job = ProactiveFollowupJob(
        api_client=api,
        clock=lambda: _NOON_UTC,
        project_tz_lookup=lambda _pid: "UTC",
    )
    result = await job.run()
    assert result.errors


@pytest.mark.asyncio
async def test_one_bad_row_does_not_kill_the_tick() -> None:
    api = _FakeApi([_row(id=1), _row(id=2)])
    api.fire = AsyncMock(side_effect=[RuntimeError("boom"), {"ok": True}])
    job = ProactiveFollowupJob(
        api_client=api,
        clock=lambda: _NOON_UTC,
        project_tz_lookup=lambda _pid: "UTC",
    )
    result = await job.run()
    # First row errored; second row fired.
    assert result.fired == 1
    assert result.errors
