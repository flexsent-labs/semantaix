"""Epic 16 — operator self-registration API journey (no live Telegram)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.api.app.main import app as api_app
from services.api.app.main import (
    operator_registration_repository,
    operator_repository,
    project_repository,
    settings,
    telegram_bot_sender,
)

_INTERNAL_TOKEN = "test-internal-token"
_ADMIN_INTERNAL_TOKEN = "test-admin-internal-token"


@pytest.fixture
def api_client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "internal_service_token", _INTERNAL_TOKEN)
    monkeypatch.setattr(settings, "admin_internal_token", _ADMIN_INTERNAL_TOKEN)
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=9001))
    operator_registration_repository.db_path = str(tmp_path / "operators.db")
    operator_repository.db_path = str(tmp_path / "operators.db")
    operator_registration_repository.init_schema()
    operator_repository.init_schema()
    project_repository.db_path = str(tmp_path / "projects.db")
    project_repository.init_schema()
    project_repository.ensure_default_project()
    return TestClient(api_app)


@pytest.mark.e2e
@pytest.mark.epic("16")
def test_register_request_approve_creates_operator(api_client) -> None:
    create = api_client.post(
        "/operators/register-request",
        headers={"Authorization": f"Bearer {_INTERNAL_TOKEN}"},
        json={
            "username": "@new_operator",
            "chat_id": 424242,
            "display_name": "Иван",
        },
    )
    assert create.status_code == 200
    request_id = create.json()["request_id"]

    approve = api_client.post(
        f"/operators/register-requests/{request_id}/approve",
        headers={"X-Internal-Token": _ADMIN_INTERNAL_TOKEN},
        json={},
    )
    assert approve.status_code == 200
    assert approve.json()["username"] == "@new_operator"

    operator = operator_repository.find_by_username("@new_operator")
    assert operator is not None
    assert operator.chat_id == 424242

    events = operator_registration_repository.list_onboarding_events(
        operator_id=operator.id
    )
    event_types = [event_type for event_type, _ in events]
    assert "approved" in event_types
    assert "onboarding_sent" in event_types
