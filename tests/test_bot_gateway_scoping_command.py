"""Story 12.18 — the `/scoping` admin command sets per-service / project anketas."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.api.app.projects import ProjectRepository
from services.api.app.sales.scoping_schema import (
    CONSULTATION_SCHEMA,
    TRANSFER_SCHEMA,
)
from services.api.app.sales.scoping_schema_repository import (
    PROJECT_DEFAULT_SERVICE_ID,
    ScopingSchemaRepository,
)
from services.api.app.sales.services_repository import ServicesRepository
from services.bot_gateway.app import main as bot_main
from services.bot_gateway.app.main import app as bot_app
from services.bot_gateway.app.webhook_dedup import WebhookUpdateClaimRepository

_ADMIN = "@ajdevy"
_NOW = datetime(2026, 5, 30, tzinfo=UTC)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    sales_db = str(tmp_path / "sales.db")
    schema_repo = ScopingSchemaRepository(db_path=sales_db)
    services_repo = ServicesRepository(db_path=sales_db)
    project_repo = ProjectRepository(db_path=str(tmp_path / "projects.db"))
    monkeypatch.setattr(bot_main, "_scoping_schema_repository", schema_repo)
    monkeypatch.setattr(bot_main, "_sales_services_repository", services_repo)
    monkeypatch.setattr(bot_main, "_project_repository", project_repo)
    monkeypatch.setattr(bot_main.settings, "hitl_config_admin_username", _ADMIN)
    bot_main.hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    monkeypatch.setenv("PERSISTENCE_DB_PATH", str(tmp_path / "persistence.sqlite3"))
    # Isolate dedup store so update_ids don't collide across tests in full suite.
    monkeypatch.setattr(
        bot_main,
        "webhook_update_claim_repository",
        WebhookUpdateClaimRepository(str(tmp_path / "dedup.sqlite3")),
    )
    monkeypatch.setattr(
        bot_main.api_client,
        "find_operator_by_username",
        AsyncMock(return_value=None),
    )
    return schema_repo, services_repo, project_repo


def _run(text: str, username: str = _ADMIN) -> dict | None:
    return bot_main._handle_admin_scoping_command(username=username, text=text)


def test_non_scoping_text_returns_none(wired) -> None:
    assert _run("привет") is None


def test_non_admin_is_rejected(wired) -> None:
    assert _run("/scoping default consultation", username="@stranger") == {
        "status": "ignored",
        "reason": "unauthorized_scoping",
    }


def test_invalid_format_is_rejected(wired) -> None:
    assert _run("/scoping default")["reason"] == "invalid_scoping_format"


def test_unknown_preset_is_rejected(wired) -> None:
    assert _run("/scoping default bogus")["reason"] == "unknown_preset"


def test_sets_project_default_to_preset(wired) -> None:
    schema_repo, _services, project_repo = wired
    result = _run("/scoping default consultation")
    assert result == {
        "status": "configured", "scope": "default", "preset": "consultation",
    }
    pid = project_repo.ensure_default_project().id
    assert (
        schema_repo.get_schema(
            project_id=pid, service_id=PROJECT_DEFAULT_SERVICE_ID
        )
        == CONSULTATION_SCHEMA
    )


def test_unknown_service_is_rejected(wired) -> None:
    assert _run("/scoping Багги-тур consultation")["reason"] == "unknown_service"


def test_sets_named_service_to_preset(wired) -> None:
    schema_repo, services_repo, project_repo = wired
    pid = project_repo.ensure_default_project().id
    services_repo.add(project_id=pid, name="Багги-тур", now=_NOW)

    result = _run("/scoping Багги-тур transfer")

    assert result["status"] == "configured"
    service = services_repo.get_by_name(project_id=pid, name="Багги-тур")
    assert (
        schema_repo.get_schema(project_id=pid, service_id=service.id)
        == TRANSFER_SCHEMA
    )


def test_scoping_command_via_webhook_returns_configured(wired) -> None:
    # Drives the full webhook dispatch (not just the handler) so the command
    # is wired into the inbound flow.
    schema_repo, _services, project_repo = wired
    client = TestClient(bot_app)

    response = client.post(
        "/telegram/webhook",
        json={
            "update_id": 9001,
            "message": {
                "message_id": 1,
                "from": {"id": 1, "username": "ajdevy"},
                "chat": {"id": 1, "type": "private"},
                "text": "/scoping default consultation",
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "configured"
    assert body["preset"] == "consultation"
    pid = project_repo.ensure_default_project().id
    assert (
        schema_repo.get_schema(
            project_id=pid, service_id=PROJECT_DEFAULT_SERVICE_ID
        )
        == CONSULTATION_SCHEMA
    )
