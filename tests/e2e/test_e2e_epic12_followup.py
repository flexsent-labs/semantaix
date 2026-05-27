"""Epic 12 Story 12.08: proactive +1d follow-up end-to-end."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.main import app as api_app
from services.api.app.sales.followup_queue_repository import (
    STATUS_CANCELLED_REPLIED,
    STATUS_SENT,
    STATUS_SKIPPED_STALE,
    FollowupQueueRepository,
)
from services.api.app.sales.state_repository import StateRepository
from services.scheduler.app.jobs.proactive_followup import ProactiveFollowupJob

pytestmark = [pytest.mark.e2e, pytest.mark.epic("12"), pytest.mark.story("12-08")]


_TOKEN = "e2e-internal-token"
_MSK = ZoneInfo("Europe/Moscow")


class _TestClientApiClient:
    def __init__(self, client: TestClient) -> None:
        self._client = client
        self._headers = {"Authorization": f"Bearer {_TOKEN}"}

    async def list_due_followups(
        self, *, now: datetime
    ) -> list[dict[str, Any]]:
        response = self._client.get(
            "/sales/followups/due",
            params={"now": now.isoformat()},
            headers=self._headers,
        )
        response.raise_for_status()
        return response.json()["rows"]

    async def skip_stale(self, followup_id: int) -> None:
        response = self._client.post(
            f"/sales/followups/{followup_id}/skip-stale", headers=self._headers
        )
        response.raise_for_status()

    async def reschedule(
        self, followup_id: int, *, new_fire_at: datetime
    ) -> None:
        response = self._client.post(
            f"/sales/followups/{followup_id}/reschedule",
            json={"new_fire_at": new_fire_at.isoformat()},
            headers=self._headers,
        )
        response.raise_for_status()

    async def fire(self, followup_id: int) -> dict[str, Any]:
        response = self._client.post(
            f"/sales/followups/{followup_id}/fire", headers=self._headers
        )
        response.raise_for_status()
        return response.json()


@pytest.fixture
def env(tmp_path, monkeypatch):
    sales_db = tmp_path / "sales.sqlite3"
    state_repo = StateRepository(db_path=str(sales_db))
    followup_repo = FollowupQueueRepository(db_path=str(sales_db))
    monkeypatch.setattr(api_main.settings, "internal_service_token", _TOKEN)
    monkeypatch.setattr(api_main, "sales_state_repository", state_repo)
    monkeypatch.setattr(api_main, "sales_followup_repository", followup_repo)
    monkeypatch.setattr(
        api_main.sales_followup_fire_handler, "_followup_repo", followup_repo
    )
    monkeypatch.setattr(
        api_main.sales_followup_fire_handler, "_state_repo", state_repo
    )
    send_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(
        api_main.sales_followup_fire_handler,
        "_telegram_sender",
        type("_S", (), {"send_message": send_mock})(),
    )
    monkeypatch.setattr(
        api_main.sales_followup_fire_handler,
        "_openrouter",
        type("_R", (), {
            "complete_json": AsyncMock(
                return_value={"text": "Привет, остались вопросы?"}
            )
        })(),
    )
    return {
        "client": TestClient(api_app),
        "state_repo": state_repo,
        "followup_repo": followup_repo,
        "send_mock": send_mock,
    }


def _job(env: dict[str, Any], now: datetime) -> ProactiveFollowupJob:
    return ProactiveFollowupJob(
        api_client=_TestClientApiClient(env["client"]),
        clock=lambda: now,
        project_tz_lookup=lambda _pid: "Europe/Moscow",
    )


@pytest.mark.asyncio
async def test_fires_in_window(env: dict[str, Any]) -> None:
    repo: FollowupQueueRepository = env["followup_repo"]
    now0 = datetime(2026, 5, 26, 8, 0, tzinfo=UTC)  # 11:00 MSK
    row_id = repo.enqueue(
        chat_id=42, project_id=1, fire_at=now0, now=now0
    )
    # Advance one hour: the row is past fire_at and we are outside quiet hours.
    later = now0 + timedelta(hours=1)
    result = await _job(env, later).run()
    assert result.fired == 1
    env["send_mock"].assert_awaited_once()
    assert repo.get(row_id) is not None
    assert repo.get(row_id).status == STATUS_SENT  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_cancel_on_reply(env: dict[str, Any]) -> None:
    repo: FollowupQueueRepository = env["followup_repo"]
    now0 = datetime(2026, 5, 26, 8, 0, tzinfo=UTC)
    row_id = repo.enqueue(
        chat_id=42, project_id=1, fire_at=now0 + timedelta(hours=24), now=now0
    )
    # Customer replies 12h in — cancel.
    cancelled = repo.mark_cancelled_replied(42, now=now0 + timedelta(hours=12))
    assert cancelled == 1
    # Tick at T+25h finds no due rows.
    later = now0 + timedelta(hours=25)
    result = await _job(env, later).run()
    assert result.total_processed() == 0
    env["send_mock"].assert_not_awaited()
    row = repo.get(row_id)
    assert row is not None
    assert row.status == STATUS_CANCELLED_REPLIED


@pytest.mark.asyncio
async def test_skip_if_stale(env: dict[str, Any]) -> None:
    repo: FollowupQueueRepository = env["followup_repo"]
    state_repo: StateRepository = env["state_repo"]
    now = datetime(2026, 5, 26, 8, 0, tzinfo=UTC)
    state_repo.upsert(
        chat_id=42,
        project_id=1,
        current_stage="scoping",
        collected_intent={"dates": "20 апреля"},
        now=now,
    )
    repo.enqueue(
        chat_id=42, project_id=1, fire_at=now - timedelta(hours=1), now=now
    )
    result = await _job(env, now).run()
    assert result.skipped_stale == 1
    env["send_mock"].assert_not_awaited()
    rows = repo.list_for_chat(42)
    assert rows[0].status == STATUS_SKIPPED_STALE


@pytest.mark.asyncio
async def test_quiet_hours_reschedule_then_fire(env: dict[str, Any]) -> None:
    repo: FollowupQueueRepository = env["followup_repo"]
    # 22:00 MSK == 19:00 UTC; row enqueued the day prior so it's due.
    quiet_now = datetime(2026, 5, 26, 19, 0, tzinfo=UTC)
    row_id = repo.enqueue(
        chat_id=42,
        project_id=1,
        fire_at=quiet_now - timedelta(hours=1),
        now=quiet_now,
    )
    result_quiet = await _job(env, quiet_now).run()
    assert result_quiet.rescheduled == 1
    rescheduled = repo.get(row_id)
    assert rescheduled is not None
    assert rescheduled.fire_at.astimezone(_MSK) == datetime(
        2026, 5, 27, 10, 0, tzinfo=_MSK
    )

    # Tick again at 10:01 MSK — fires.
    morning = datetime(2026, 5, 27, 7, 1, tzinfo=UTC)
    result_morning = await _job(env, morning).run()
    assert result_morning.fired == 1
    env["send_mock"].assert_awaited_once()
    final = repo.get(row_id)
    assert final is not None
    assert final.status == STATUS_SENT
