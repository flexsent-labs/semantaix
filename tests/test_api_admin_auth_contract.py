from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.admin_auth import AdminAuthRepository
from services.api.app.operators import OperatorRepository
from services.api.app.projects import ProjectRepository


@pytest.fixture
def admin_stack(tmp_path, monkeypatch):
    """Wire fresh project/operator/admin_auth singletons + a mocked Telegram sender.

    Yields ``(client, send_mock, fresh_admin_repo)``.
    """
    projects = ProjectRepository(str(tmp_path / "projects.sqlite3"))
    operators = OperatorRepository(str(tmp_path / "operators.sqlite3"))
    admin_auth = AdminAuthRepository(str(tmp_path / "admin.sqlite3"))
    projects.ensure_default_project()
    monkeypatch.setattr(api_main, "project_repository", projects)
    monkeypatch.setattr(api_main, "operator_repository", operators)
    monkeypatch.setattr(api_main, "admin_auth_repository", admin_auth)
    monkeypatch.setattr(api_main.settings, "admin_telegram_username", "@admin")
    monkeypatch.setattr(api_main.settings, "hitl_config_admin_username", "@admin")
    monkeypatch.setattr(api_main.settings, "telegram_alert_chat_id", "99")
    monkeypatch.setattr(api_main.settings, "admin_login_code_ttl_seconds", 300)
    monkeypatch.setattr(api_main.settings, "admin_session_ttl_seconds", 86400)
    send_mock = AsyncMock(return_value=42)
    monkeypatch.setattr(api_main.telegram_bot_sender, "bot_token", "real-token")
    monkeypatch.setattr(api_main.telegram_bot_sender, "send_message", send_mock)

    client = TestClient(api_main.app)
    yield client, send_mock, admin_auth


def test_login_request_happy_path_dms_code(admin_stack):
    client, send_mock, _ = admin_stack
    response = client.post(
        "/admin/login/request", json={"admin_username": "@admin"}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"requested": True}
    send_mock.assert_awaited_once()
    kwargs = send_mock.await_args.kwargs
    assert kwargs["chat_id"] == 99
    # Code text contains a 6-digit code.
    assert any(ch.isdigit() for ch in kwargs["text"])


def test_login_request_rejects_non_admin_username(admin_stack):
    client, send_mock, _ = admin_stack
    response = client.post(
        "/admin/login/request", json={"admin_username": "@stranger"}
    )
    assert response.status_code == 403
    send_mock.assert_not_awaited()


def test_login_request_fails_without_admin_chat_id(admin_stack, monkeypatch):
    client, send_mock, _ = admin_stack
    monkeypatch.setattr(api_main.settings, "telegram_alert_chat_id", None)
    response = client.post(
        "/admin/login/request", json={"admin_username": "@admin"}
    )
    assert response.status_code == 404
    send_mock.assert_not_awaited()


def test_login_request_fails_when_admin_operator_missing(admin_stack):
    client, send_mock, _ = admin_stack
    # Mark admin operator inactive — find_by_username still returns row, but
    # let's instead simulate "no admin operator registered" by replacing the
    # operator repository with an empty one.
    empty = OperatorRepository(api_main.operator_repository.db_path + ".empty")
    api_main.operator_repository = empty  # type: ignore[assignment]
    try:
        response = client.post(
            "/admin/login/request", json={"admin_username": "@admin"}
        )
        assert response.status_code == 400
        send_mock.assert_not_awaited()
    finally:
        # Reset is handled by monkeypatch teardown via admin_stack fixture
        # rebinding on the next test.
        pass


def test_login_verify_round_trip_returns_session(admin_stack):
    client, send_mock, admin_auth = admin_stack
    request_response = client.post(
        "/admin/login/request", json={"admin_username": "@admin"}
    )
    assert request_response.status_code == 200
    # Extract the plaintext code from the captured Telegram DM text.
    sent_text = send_mock.await_args.kwargs["text"]
    code = "".join(ch for ch in sent_text if ch.isdigit())[:6]

    verify_response = client.post(
        "/admin/login/verify",
        json={"admin_username": "@admin", "code": code},
    )
    assert verify_response.status_code == 200, verify_response.text
    body = verify_response.json()
    assert body["session_token"]
    assert body["expires_at"]
    session = admin_auth.validate_session(body["session_token"])
    assert session is not None
    assert session.admin_username == "@admin"


def test_login_verify_wrong_code_returns_401(admin_stack):
    client, _, _ = admin_stack
    client.post("/admin/login/request", json={"admin_username": "@admin"})
    response = client.post(
        "/admin/login/verify",
        json={"admin_username": "@admin", "code": "000000"},
    )
    assert response.status_code == 401


def test_login_verify_replay_returns_401(admin_stack):
    client, send_mock, _ = admin_stack
    client.post("/admin/login/request", json={"admin_username": "@admin"})
    sent_text = send_mock.await_args.kwargs["text"]
    code = "".join(ch for ch in sent_text if ch.isdigit())[:6]
    first = client.post(
        "/admin/login/verify",
        json={"admin_username": "@admin", "code": code},
    )
    assert first.status_code == 200
    second = client.post(
        "/admin/login/verify",
        json={"admin_username": "@admin", "code": code},
    )
    assert second.status_code == 401


def test_login_verify_expired_code_returns_401(admin_stack, tmp_path):
    client, send_mock, admin_auth = admin_stack
    client.post("/admin/login/request", json={"admin_username": "@admin"})
    sent_text = send_mock.await_args.kwargs["text"]
    code = "".join(ch for ch in sent_text if ch.isdigit())[:6]
    # Force the code to be expired in storage.
    past = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    import sqlite3

    with sqlite3.connect(admin_auth.db_path) as connection:
        connection.execute(
            "UPDATE admin_login_codes SET expires_at = ? WHERE admin_username = ?",
            (past, "@admin"),
        )
    response = client.post(
        "/admin/login/verify",
        json={"admin_username": "@admin", "code": code},
    )
    assert response.status_code == 401


def test_login_verify_rejects_non_admin_username(admin_stack):
    client, _, _ = admin_stack
    response = client.post(
        "/admin/login/verify",
        json={"admin_username": "@stranger", "code": "123456"},
    )
    assert response.status_code == 403


def test_session_check_valid_session_returns_200(admin_stack):
    client, send_mock, _ = admin_stack
    client.post("/admin/login/request", json={"admin_username": "@admin"})
    code = "".join(
        ch for ch in send_mock.await_args.kwargs["text"] if ch.isdigit()
    )[:6]
    body = client.post(
        "/admin/login/verify",
        json={"admin_username": "@admin", "code": code},
    ).json()
    token = body["session_token"]
    check = client.get(
        "/admin/session/check", headers={"X-Admin-Session": token}
    )
    assert check.status_code == 200
    assert check.json()["admin_username"] == "@admin"


def test_session_check_missing_header_returns_401(admin_stack):
    client, _, _ = admin_stack
    response = client.get("/admin/session/check")
    assert response.status_code == 401


def test_session_check_invalid_token_returns_401(admin_stack):
    client, _, _ = admin_stack
    response = client.get(
        "/admin/session/check", headers={"X-Admin-Session": "garbage"}
    )
    assert response.status_code == 401


def test_logout_revokes_session(admin_stack):
    client, send_mock, _ = admin_stack
    client.post("/admin/login/request", json={"admin_username": "@admin"})
    code = "".join(
        ch for ch in send_mock.await_args.kwargs["text"] if ch.isdigit()
    )[:6]
    token = client.post(
        "/admin/login/verify",
        json={"admin_username": "@admin", "code": code},
    ).json()["session_token"]
    logout = client.post(
        "/admin/logout", headers={"X-Admin-Session": token}
    )
    assert logout.status_code == 200
    follow_up = client.get(
        "/admin/session/check", headers={"X-Admin-Session": token}
    )
    assert follow_up.status_code == 401


def test_logout_without_header_returns_401(admin_stack):
    client, _, _ = admin_stack
    response = client.post("/admin/logout")
    assert response.status_code == 401


def test_login_request_telegram_send_failure_returns_502(admin_stack, monkeypatch):
    client, send_mock, _ = admin_stack
    send_mock.side_effect = RuntimeError("telegram down")
    response = client.post(
        "/admin/login/request", json={"admin_username": "@admin"}
    )
    assert response.status_code == 502
    assert "telegram" in response.json()["detail"].lower()
