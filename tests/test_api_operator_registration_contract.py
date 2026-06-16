from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

import services.api.app.main as api_main
from services.api.app.main import (
    app as api_app,
)
from services.api.app.main import (
    operator_registration_repository,
    operator_repository,
    project_repository,
    telegram_bot_sender,
)


def _wire(tmp_path) -> None:
    operator_db = str(tmp_path / "operators.sqlite3")
    projects_db = str(tmp_path / "projects.sqlite3")
    operator_repository.db_path = operator_db
    operator_repository.init_schema()
    operator_registration_repository.db_path = operator_db
    operator_registration_repository.init_schema()
    project_repository.db_path = projects_db
    project_repository.init_schema()
    default_project = project_repository.ensure_default_project()
    operator_repository.ensure_default_operator(
        username=api_main.settings.telegram_alert_username,
        project_id=default_project.id,
        chat_id=445566,
    )


def _set_auth_tokens(monkeypatch) -> tuple[str, str]:
    internal = "internal-token"
    admin_internal = "admin-internal-token"
    monkeypatch.setattr(api_main.settings, "internal_service_token", internal)
    monkeypatch.setattr(api_main.settings, "admin_internal_token", admin_internal)
    return internal, admin_internal


def test_create_and_list_registration_requests(tmp_path, monkeypatch):
    _wire(tmp_path)
    internal_token, admin_internal_token = _set_auth_tokens(monkeypatch)
    mock_send = AsyncMock(return_value=1001)
    monkeypatch.setattr(telegram_bot_sender, "send_message", mock_send)
    client = TestClient(api_app)

    create = client.post(
        "/operators/register-request",
        headers={"Authorization": f"Bearer {internal_token}"},
        json={"username": "@newop", "chat_id": 7001, "display_name": "New Operator"},
    )
    assert create.status_code == 200
    body = create.json()
    assert body["status"] == "pending"
    request_id = body["request_id"]

    listed = client.get(
        "/operators/register-requests",
        headers={"x-internal-token": admin_internal_token},
    )
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == request_id
    assert items[0]["username"] == "@newop"

    _, kwargs = mock_send.await_args
    assert kwargs["chat_id"] == int(api_main._effective_hitl_operator_chat_id())
    markup = kwargs["reply_markup"]
    first_row = markup["inline_keyboard"][0]
    assert first_row[0]["callback_data"] == f"op_reg:approve:{request_id}"
    assert first_row[1]["callback_data"] == f"op_reg:reject:{request_id}"


def test_approve_registration_sends_onboarding_and_records_event(tmp_path, monkeypatch):
    _wire(tmp_path)
    internal_token, admin_internal_token = _set_auth_tokens(monkeypatch)
    mock_send = AsyncMock(return_value=2002)
    monkeypatch.setattr(telegram_bot_sender, "send_message", mock_send)
    client = TestClient(api_app)

    created = client.post(
        "/operators/register-request",
        headers={"Authorization": f"Bearer {internal_token}"},
        json={"username": "@approveop", "chat_id": 8123},
    )
    request_id = created.json()["request_id"]
    approved = client.post(
        f"/operators/register-requests/{request_id}/approve",
        headers={"x-internal-token": admin_internal_token},
        json={},
    )
    assert approved.status_code == 200
    operator_id = approved.json()["id"]
    events = operator_registration_repository.list_onboarding_events(operator_id=operator_id)
    assert [event for event, _ in events] == ["approved", "onboarding_sent"]

    _, kwargs = mock_send.await_args
    keyboard_row = kwargs["reply_markup"]["inline_keyboard"][0]
    assert keyboard_row[0]["callback_data"] == f"onboard:cal:{operator_id}"
    assert keyboard_row[1]["callback_data"] == f"onboard:tg:{operator_id}"


def test_reject_and_notify_registration(tmp_path, monkeypatch):
    _wire(tmp_path)
    internal_token, admin_internal_token = _set_auth_tokens(monkeypatch)
    mock_send = AsyncMock(return_value=3003)
    monkeypatch.setattr(telegram_bot_sender, "send_message", mock_send)
    client = TestClient(api_app)

    created = client.post(
        "/operators/register-request",
        headers={"Authorization": f"Bearer {internal_token}"},
        json={"username": "@rejectop", "chat_id": 9191},
    )
    request_id = created.json()["request_id"]

    rejected = client.post(
        f"/operators/register-requests/{request_id}/reject",
        headers={"x-internal-token": admin_internal_token},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"

    notified = client.post(
        f"/operators/register-requests/{request_id}/reject-notify",
        headers={"Authorization": f"Bearer {internal_token}"},
    )
    assert notified.status_code == 200
    assert notified.json()["notified"] is True


def test_register_request_rejects_already_operator(tmp_path, monkeypatch):
    _wire(tmp_path)
    internal_token, _ = _set_auth_tokens(monkeypatch)
    client = TestClient(api_app)
    operator_repository.create(
        username="@existing",
        project_id=project_repository.ensure_default_project().id,
        chat_id=1,
    )
    response = client.post(
        "/operators/register-request",
        headers={"Authorization": f"Bearer {internal_token}"},
        json={"username": "@existing", "chat_id": 2},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "already_operator"


def test_register_request_pending_and_cooldown_conflicts(tmp_path, monkeypatch):
    _wire(tmp_path)
    internal_token, admin_token = _set_auth_tokens(monkeypatch)
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))
    client = TestClient(api_app)

    first = client.post(
        "/operators/register-request",
        headers={"Authorization": f"Bearer {internal_token}"},
        json={"username": "@pending", "chat_id": 1},
    )
    assert first.status_code == 200
    dup = client.post(
        "/operators/register-request",
        headers={"Authorization": f"Bearer {internal_token}"},
        json={"username": "@pending", "chat_id": 1},
    )
    assert dup.status_code == 409
    assert dup.json()["detail"] == "registration_pending"

    request_id = first.json()["request_id"]
    client.post(
        f"/operators/register-requests/{request_id}/reject",
        headers={"x-internal-token": admin_token},
    )
    cooled = client.post(
        "/operators/register-request",
        headers={"Authorization": f"Bearer {internal_token}"},
        json={"username": "@pending", "chat_id": 1},
    )
    assert cooled.status_code == 409
    assert cooled.json()["detail"] == "registration_cooldown"


def test_approve_and_reject_error_paths(tmp_path, monkeypatch):
    _wire(tmp_path)
    internal_token, admin_token = _set_auth_tokens(monkeypatch)
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))
    client = TestClient(api_app)

    missing = client.post(
        "/operators/register-requests/999/approve",
        headers={"x-internal-token": admin_token},
        json={},
    )
    assert missing.status_code == 404

    created = client.post(
        "/operators/register-request",
        headers={"Authorization": f"Bearer {internal_token}"},
        json={"username": "@once", "chat_id": 55},
    )
    request_id = created.json()["request_id"]
    client.post(
        f"/operators/register-requests/{request_id}/reject",
        headers={"x-internal-token": admin_token},
    )
    twice = client.post(
        f"/operators/register-requests/{request_id}/approve",
        headers={"x-internal-token": admin_token},
        json={},
    )
    assert twice.status_code == 409
    assert twice.json()["detail"] == "request_not_pending"


def test_onboarding_notify_and_get_operator_by_id(tmp_path, monkeypatch):
    _wire(tmp_path)
    internal_token, admin_token = _set_auth_tokens(monkeypatch)
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))
    client = TestClient(api_app)

    created = client.post(
        "/operators/register-request",
        headers={"Authorization": f"Bearer {internal_token}"},
        json={"username": "@notifyop", "chat_id": 444},
    )
    request_id = created.json()["request_id"]
    approved = client.post(
        f"/operators/register-requests/{request_id}/approve",
        headers={"x-internal-token": admin_token},
        json={},
    )
    operator_id = approved.json()["id"]

    notify = client.post(
        f"/operators/register-requests/{request_id}/onboarding-notify",
        headers={"Authorization": f"Bearer {internal_token}"},
    )
    assert notify.status_code == 200

    by_id = client.get(
        f"/operators/id/{operator_id}",
        headers={"Authorization": f"Bearer {internal_token}"},
    )
    assert by_id.status_code == 200
    assert by_id.json()["username"] == "@notifyop"

    event = client.post(
        f"/operators/{operator_id}/onboarding-events",
        headers={"Authorization": f"Bearer {internal_token}"},
        json={"event_type": "calendar_started"},
    )
    assert event.status_code == 200

    bad_notify = client.post(
        "/operators/register-requests/999/onboarding-notify",
        headers={"Authorization": f"Bearer {internal_token}"},
    )
    assert bad_notify.status_code == 404
