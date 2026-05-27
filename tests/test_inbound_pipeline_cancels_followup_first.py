"""``/conversations/inbound`` cancels pending follow-ups BEFORE the pipeline.

Ordering matters: if the answerer turn re-enqueues (which it does on
``handled=True``) the cancel must fire first so we don't accidentally
cancel the fresh queue row the answerer just inserted.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.answerers import AnswerResult
from services.api.app.main import app as api_app
from services.api.app.sales.followup_queue_repository import (
    FollowupQueueRepository,
)


@pytest.fixture
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
    sales_db = tmp_path / "sales.sqlite3"
    followup_repo = FollowupQueueRepository(db_path=str(sales_db))
    monkeypatch.setattr(api_main, "sales_followup_repository", followup_repo)
    yield {"client": TestClient(api_app), "repo": followup_repo}


def test_cancel_runs_before_pipeline(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client: TestClient = env["client"]
    repo: FollowupQueueRepository = env["repo"]

    call_log: list[str] = []
    original_cancel = api_main.maybe_cancel
    original_run = api_main.answer_pipeline.run

    def recording_cancel(*args: Any, **kwargs: Any) -> int:
        call_log.append("cancel")
        return original_cancel(*args, **kwargs)

    async def recording_run(*args: Any, **kwargs: Any) -> AnswerResult:
        call_log.append("pipeline")
        return await original_run(*args, **kwargs)

    monkeypatch.setattr(api_main, "maybe_cancel", recording_cancel)
    monkeypatch.setattr(api_main.answer_pipeline, "run", recording_run)
    monkeypatch.setattr(
        api_main.telegram_bot_sender,
        "send_message",
        AsyncMock(return_value=1),
    )

    # Seed a scheduled follow-up so the cancel has something to do.
    now = datetime.now(UTC)
    repo.enqueue(
        chat_id=4242, project_id=1, fire_at=now + timedelta(hours=24), now=now
    )

    response = client.post(
        "/conversations/inbound",
        json={
            "chat_id": 4242,
            "customer_username": "@darya",
            "text": "ещё подумаю",
        },
    )
    assert response.status_code == 200
    assert call_log[:2] == ["cancel", "pipeline"]
