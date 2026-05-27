"""End-to-end: scheduler job + api endpoints + repo against a real TestClient."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.main import app as api_app
from services.api.app.sales.followup_queue_repository import (
    STATUS_SENT,
    STATUS_SKIPPED_STALE,
    FollowupQueueRepository,
)
from services.api.app.sales.state_repository import StateRepository
from services.scheduler.app.jobs.proactive_followup import ProactiveFollowupJob

_TOKEN = "test-internal-token"


class _TestClientApiClient:
    """Tiny adapter so the job can talk to the FastAPI TestClient."""

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
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
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
    send_mock = AsyncMock(return_value=99)
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
    client = TestClient(api_app)
    yield {
        "client": client,
        "state_repo": state_repo,
        "followup_repo": followup_repo,
        "send_mock": send_mock,
    }


@pytest.mark.asyncio
async def test_e2e_fires_due_row(env: dict[str, Any]) -> None:
    repo: FollowupQueueRepository = env["followup_repo"]
    now = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)
    row_id = repo.enqueue(
        chat_id=42, project_id=1, fire_at=now - timedelta(hours=1), now=now
    )

    job = ProactiveFollowupJob(
        api_client=_TestClientApiClient(env["client"]),
        clock=lambda: now,
        project_tz_lookup=lambda _pid: "UTC",
    )
    result = await job.run()
    assert result.fired == 1
    env["send_mock"].assert_awaited_once()
    row = repo.get(row_id)
    assert row is not None
    assert row.status == STATUS_SENT


@pytest.mark.asyncio
async def test_e2e_skips_stale(env: dict[str, Any]) -> None:
    repo: FollowupQueueRepository = env["followup_repo"]
    state_repo: StateRepository = env["state_repo"]
    now = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)
    state_repo.upsert(
        chat_id=42,
        project_id=1,
        current_stage="scoping",
        collected_intent={"dates": "20 апреля"},
        now=now,
    )
    row_id = repo.enqueue(
        chat_id=42, project_id=1, fire_at=now - timedelta(hours=1), now=now
    )

    job = ProactiveFollowupJob(
        api_client=_TestClientApiClient(env["client"]),
        clock=lambda: now,
        project_tz_lookup=lambda _pid: "UTC",
    )
    result = await job.run()
    assert result.skipped_stale == 1
    assert result.fired == 0
    env["send_mock"].assert_not_awaited()
    row = repo.get(row_id)
    assert row is not None
    assert row.status == STATUS_SKIPPED_STALE
    assert row.reason == "past_intent_date"
