"""Epic 10 story 10.02: admin login round-trip via the api with a mocked DM.

Drives the full sequence: request code → bot DM intercept → verify →
session check → logout → re-check fails. The Telegram sender is mocked so
the test never touches the network.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.admin_auth import AdminAuthRepository
from services.api.app.operators import OperatorRepository
from services.api.app.projects import ProjectRepository

pytestmark = [pytest.mark.e2e, pytest.mark.epic("10")]


@pytest.mark.story("10-02")
def test_admin_login_round_trip(tmp_path, monkeypatch):
    projects = ProjectRepository(str(tmp_path / "projects.sqlite3"))
    operators = OperatorRepository(str(tmp_path / "operators.sqlite3"))
    admin_auth = AdminAuthRepository(str(tmp_path / "admin.sqlite3"))
    monkeypatch.setattr(api_main, "project_repository", projects)
    monkeypatch.setattr(api_main, "operator_repository", operators)
    monkeypatch.setattr(api_main, "admin_auth_repository", admin_auth)
    monkeypatch.setattr(api_main.settings, "admin_telegram_username", "@e2e_admin")
    monkeypatch.setattr(api_main.settings, "telegram_alert_chat_id", "555")
    monkeypatch.setattr(api_main.settings, "admin_login_code_ttl_seconds", 300)
    monkeypatch.setattr(api_main.settings, "admin_session_ttl_seconds", 3600)
    send_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(api_main.telegram_bot_sender, "bot_token", "real-token")
    monkeypatch.setattr(api_main.telegram_bot_sender, "send_message", send_mock)

    client = TestClient(api_main.app)

    request_response = client.post(
        "/admin/login/request", json={"admin_username": "@e2e_admin"}
    )
    assert request_response.status_code == 200
    send_mock.assert_awaited_once()
    dm_text = send_mock.await_args.kwargs["text"]
    assert send_mock.await_args.kwargs["chat_id"] == 555
    code = "".join(ch for ch in dm_text if ch.isdigit())[:6]
    assert len(code) == 6

    verify_response = client.post(
        "/admin/login/verify",
        json={"admin_username": "@e2e_admin", "code": code},
    )
    assert verify_response.status_code == 200
    token = verify_response.json()["session_token"]

    check_response = client.get(
        "/admin/session/check", headers={"X-Admin-Session": token}
    )
    assert check_response.status_code == 200
    assert check_response.json()["admin_username"] == "@e2e_admin"

    logout_response = client.post(
        "/admin/logout", headers={"X-Admin-Session": token}
    )
    assert logout_response.status_code == 200

    post_logout = client.get(
        "/admin/session/check", headers={"X-Admin-Session": token}
    )
    assert post_logout.status_code == 401
