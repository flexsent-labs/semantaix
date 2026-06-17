"""Epic 10.5 story 10.5-03 E2E: admin login via operators table + /hitl_config with project_slug."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.e2e, pytest.mark.epic("10.5")]


@pytest.mark.story("10.5-03")
def test_admin_login_resolves_chat_id_from_operators_table(tmp_path, monkeypatch):
    """
    /admin/auth/request_code resolves the admin's chat_id from the operators table
    (not from hitl_primary_operator_chat_id, which was removed in 10.5-03).
    """
    from services.api.app import main as api_main
    from services.api.app.operators import OperatorRepository
    from services.api.app.projects import ProjectRepository
    from services.api.app.web_auth import WebAuthRepository

    proj = ProjectRepository(str(tmp_path / "proj.db"))
    ops = OperatorRepository(str(tmp_path / "ops.db"))
    web_auth = WebAuthRepository(str(tmp_path / "web_auth.db"))
    default = proj.ensure_default_project()

    ops.create(username="@e2e-admin", project_id=default.id, chat_id=7777)

    monkeypatch.setattr(api_main, "project_repository", proj)
    monkeypatch.setattr(api_main, "operator_repository", ops)
    monkeypatch.setattr(api_main.settings, "admin_telegram_username", "@e2e-admin")
    monkeypatch.setattr(api_main.settings, "telegram_alert_chat_id", None)
    monkeypatch.setattr(api_main.settings, "operators_db_path", str(tmp_path / "ops.db"))
    monkeypatch.setattr(
        api_main.settings, "operator_files_db_path", str(tmp_path / "nofiles.db")
    )

    send_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(api_main.telegram_bot_sender, "bot_token", "stub-token")
    monkeypatch.setattr(api_main.telegram_bot_sender, "send_message", send_mock)

    # Patch the auth service that's registered on the app to use our tmp repos.
    from services.api.app.admin_auth import AdminAuthService

    service = AdminAuthService(
        settings=api_main.settings,
        web_auth_repository=web_auth,
        telegram_bot_sender=api_main.telegram_bot_sender,
    )
    with patch("services.api.app.admin_auth.AdminAuthService", return_value=service):
        client = TestClient(api_main.app)
        resp = client.post(
            "/admin/auth/request_code", json={"username": "@e2e-admin"}
        )

    assert resp.status_code == 200
    assert resp.json()["sent"] is True
    send_mock.assert_awaited_once()
    called_chat_id = send_mock.await_args.kwargs["chat_id"]
    assert called_chat_id == 7777, (
        f"Expected code sent to 7777 (from operators table) but got {called_chat_id}"
    )


@pytest.mark.story("10.5-03")
@pytest.mark.asyncio
async def test_hitl_config_with_project_slug_routes_to_correct_project(
    tmp_path, monkeypatch
):
    """
    /hitl_config @op chat_id project_slug registers the operator in the named
    project, not the default project.
    """
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    import services.bot_gateway.app.main as bot_main
    from services.api.app.hitl import HitlTicketRepository
    from services.api.app.projects import ProjectRepository
    from services.bot_gateway.app.webhook_dedup import WebhookUpdateClaimRepository

    proj = ProjectRepository(str(tmp_path / "proj.db"))
    hitl = HitlTicketRepository(str(tmp_path / "hitl.db"))
    dedup = WebhookUpdateClaimRepository(str(tmp_path / "dedup.db"))
    proj.ensure_default_project()
    salon = proj.create(slug="salon", name="Салон")

    monkeypatch.setattr(bot_main, "_project_repository", proj)
    monkeypatch.setattr(bot_main, "hitl_ticket_repository", hitl)
    monkeypatch.setattr(bot_main, "webhook_update_claim_repository", dedup)
    monkeypatch.setattr(bot_main.settings, "hitl_config_admin_username", "@admin")
    monkeypatch.setattr(bot_main.settings, "telegram_bot_token", "stub-token")

    attach_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(bot_main.api_client, "attach_operator", attach_mock)

    client = TestClient(bot_main.app)
    resp = client.post(
        "/telegram/webhook",
        json={
            "update_id": 50010,
            "message": {
                "message_id": 1,
                "from": {"id": 1, "username": "admin"},
                "chat": {"id": 1, "type": "private"},
                "text": "/hitl_config @op-salon 303 salon",
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "configured"
    assert body["operator_username"] == "@op-salon"
    assert body["telegram_alert_chat_id"] == "303"
    assert body["project_id"] == str(salon.id)

    attach_mock.assert_awaited_once()
    call_kwargs = attach_mock.await_args.kwargs
    assert call_kwargs["project_id"] == salon.id
    assert call_kwargs["username"] == "@op-salon"
    assert call_kwargs["chat_id"] == 303


@pytest.mark.story("10.5-03")
@pytest.mark.asyncio
async def test_hitl_config_unknown_project_slug_is_rejected(tmp_path, monkeypatch):
    """Unknown project_slug returns ignored/unknown_project_slug, not a 500."""
    import services.bot_gateway.app.main as bot_main
    from services.api.app.hitl import HitlTicketRepository
    from services.api.app.projects import ProjectRepository
    from services.bot_gateway.app.webhook_dedup import WebhookUpdateClaimRepository

    proj = ProjectRepository(str(tmp_path / "proj.db"))
    hitl = HitlTicketRepository(str(tmp_path / "hitl.db"))
    dedup = WebhookUpdateClaimRepository(str(tmp_path / "dedup.db"))
    proj.ensure_default_project()

    monkeypatch.setattr(bot_main, "_project_repository", proj)
    monkeypatch.setattr(bot_main, "hitl_ticket_repository", hitl)
    monkeypatch.setattr(bot_main, "webhook_update_claim_repository", dedup)
    monkeypatch.setattr(bot_main.settings, "hitl_config_admin_username", "@admin")
    monkeypatch.setattr(bot_main.settings, "telegram_bot_token", "stub-token")

    client = TestClient(bot_main.app)
    resp = client.post(
        "/telegram/webhook",
        json={
            "update_id": 50020,
            "message": {
                "message_id": 2,
                "from": {"id": 1, "username": "admin"},
                "chat": {"id": 1, "type": "private"},
                "text": "/hitl_config @op 999 ghost-project",
            },
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert resp.json()["reason"] == "unknown_project_slug"
