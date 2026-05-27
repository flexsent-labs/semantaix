"""API contract for ``/sales/followups/*`` (Story 12.08).

Covers the four service-token-gated endpoints the scheduler job calls:
``due``, ``skip-stale``, ``reschedule``, and ``fire``.
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
from services.api.app.main import app as api_app
from services.api.app.sales.followup_queue_repository import (
    STATUS_SCHEDULED,
    STATUS_SENT,
    STATUS_SKIPPED_STALE,
    FollowupQueueRepository,
)

_NOW = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)
_TOKEN = "test-internal-token"


@pytest.fixture
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
    sales_db = tmp_path / "sales.sqlite3"
    followup_repo = FollowupQueueRepository(db_path=str(sales_db))

    monkeypatch.setattr(api_main.settings, "sales_db_path", str(sales_db))
    monkeypatch.setattr(api_main.settings, "internal_service_token", _TOKEN)
    monkeypatch.setattr(api_main, "sales_followup_repository", followup_repo)
    monkeypatch.setattr(
        api_main.sales_followup_fire_handler, "_followup_repo", followup_repo
    )

    client = TestClient(api_app)
    yield {"client": client, "repo": followup_repo}


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


def test_due_returns_scheduled_rows(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    repo: FollowupQueueRepository = env["repo"]
    repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW - timedelta(seconds=1), now=_NOW
    )
    response = client.get(
        "/sales/followups/due",
        params={"now": _NOW.isoformat()},
        headers=_headers(),
    )
    assert response.status_code == 200
    rows = response.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["chat_id"] == 42
    assert rows[0]["status"] == STATUS_SCHEDULED


def test_due_caps_at_100(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    repo: FollowupQueueRepository = env["repo"]
    for chat_id in range(150):
        repo.enqueue(
            chat_id=chat_id,
            project_id=1,
            fire_at=_NOW - timedelta(seconds=chat_id + 1),
            now=_NOW,
        )
    response = client.get(
        "/sales/followups/due",
        params={"now": _NOW.isoformat()},
        headers=_headers(),
    )
    assert response.status_code == 200
    assert len(response.json()["rows"]) == 100


def test_due_requires_internal_token(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    response = client.get(
        "/sales/followups/due", params={"now": _NOW.isoformat()}
    )
    assert response.status_code == 401


def test_due_invalid_now_returns_400(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    response = client.get(
        "/sales/followups/due",
        params={"now": "not-a-date"},
        headers=_headers(),
    )
    assert response.status_code == 400


def test_due_omits_now_defaults_to_utc(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    repo: FollowupQueueRepository = env["repo"]
    repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW - timedelta(days=365), now=_NOW
    )
    response = client.get("/sales/followups/due", headers=_headers())
    assert response.status_code == 200
    assert response.json()["rows"]


def test_skip_stale_marks_row(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    repo: FollowupQueueRepository = env["repo"]
    row_id = repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW, now=_NOW
    )
    response = client.post(
        f"/sales/followups/{row_id}/skip-stale", headers=_headers()
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    row = repo.get(row_id)
    assert row is not None
    assert row.status == STATUS_SKIPPED_STALE
    assert row.reason == "past_intent_date"


def test_skip_stale_unknown_id_returns_404(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    response = client.post(
        "/sales/followups/99999/skip-stale", headers=_headers()
    )
    assert response.status_code == 404


def test_reschedule_updates_fire_at(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    repo: FollowupQueueRepository = env["repo"]
    row_id = repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW, now=_NOW
    )
    new_fire = _NOW + timedelta(hours=10)
    response = client.post(
        f"/sales/followups/{row_id}/reschedule",
        json={"new_fire_at": new_fire.isoformat()},
        headers=_headers(),
    )
    assert response.status_code == 200
    row = repo.get(row_id)
    assert row is not None
    assert row.fire_at == new_fire
    assert row.status == STATUS_SCHEDULED


def test_reschedule_unknown_id_returns_404(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    response = client.post(
        "/sales/followups/99999/reschedule",
        json={"new_fire_at": _NOW.isoformat()},
        headers=_headers(),
    )
    assert response.status_code == 404


def test_reschedule_naive_datetime_assumed_utc(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    repo: FollowupQueueRepository = env["repo"]
    row_id = repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW, now=_NOW
    )
    new_fire = datetime(2026, 5, 26, 22, 0)
    response = client.post(
        f"/sales/followups/{row_id}/reschedule",
        json={"new_fire_at": new_fire.isoformat()},
        headers=_headers(),
    )
    assert response.status_code == 200
    row = repo.get(row_id)
    assert row is not None
    assert row.fire_at == new_fire.replace(tzinfo=UTC)


def test_fire_happy_path_sends_and_marks_sent(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client: TestClient = env["client"]
    repo: FollowupQueueRepository = env["repo"]
    row_id = repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW, now=_NOW
    )
    send_mock = AsyncMock(return_value=99)
    monkeypatch.setattr(
        api_main.sales_followup_fire_handler, "_telegram_sender",
        type("_S", (), {"send_message": send_mock})(),
    )
    payload_mock = AsyncMock(return_value={"text": "Привет, остались вопросы?"})
    monkeypatch.setattr(
        api_main.sales_followup_fire_handler, "_openrouter",
        type("_R", (), {"complete_json": payload_mock})(),
    )

    response = client.post(
        f"/sales/followups/{row_id}/fire", headers=_headers()
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["sent"] is True
    assert body["fallback_text_used"] is False
    send_mock.assert_awaited_once()
    row = repo.get(row_id)
    assert row is not None
    assert row.status == STATUS_SENT


def test_fire_telegram_failure_marks_skipped_stale(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client: TestClient = env["client"]
    repo: FollowupQueueRepository = env["repo"]
    row_id = repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW, now=_NOW
    )
    monkeypatch.setattr(
        api_main.sales_followup_fire_handler, "_telegram_sender",
        type("_S", (), {
            "send_message": AsyncMock(side_effect=RuntimeError("boom"))
        })(),
    )
    monkeypatch.setattr(
        api_main.sales_followup_fire_handler, "_openrouter",
        type("_R", (), {
            "complete_json": AsyncMock(return_value={"text": "..."})
        })(),
    )

    response = client.post(
        f"/sales/followups/{row_id}/fire", headers=_headers()
    )
    assert response.status_code == 200
    body = response.json()
    assert body["sent"] is False
    row = repo.get(row_id)
    assert row is not None
    assert row.status == STATUS_SKIPPED_STALE
    assert row.reason == "telegram_send_failed"


def test_fire_uses_fallback_when_llm_returns_empty(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client: TestClient = env["client"]
    repo: FollowupQueueRepository = env["repo"]
    row_id = repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW, now=_NOW
    )
    send_mock = AsyncMock(return_value=99)
    monkeypatch.setattr(
        api_main.sales_followup_fire_handler, "_telegram_sender",
        type("_S", (), {"send_message": send_mock})(),
    )
    monkeypatch.setattr(
        api_main.sales_followup_fire_handler, "_openrouter",
        type("_R", (), {"complete_json": AsyncMock(return_value={})})(),
    )

    response = client.post(
        f"/sales/followups/{row_id}/fire", headers=_headers()
    )
    assert response.status_code == 200
    body = response.json()
    assert body["sent"] is True
    assert body["fallback_text_used"] is True
    send_mock.assert_awaited_once()


def test_fire_unknown_id_returns_404(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    response = client.post(
        "/sales/followups/99999/fire", headers=_headers()
    )
    assert response.status_code == 404
