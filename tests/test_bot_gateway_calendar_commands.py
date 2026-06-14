"""Unit tests for the operator `/connect_calendar` + `/disconnect_calendar`
commands (Epic 11, story 11.03).

Drives the bot_gateway webhook end-to-end with a fake `ApiClient`
(captures calls + returns canned responses) and a fake `_send_dm`
(captures operator DMs), mirroring the existing bot_gateway command tests.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from services.bot_gateway.app import main as bot_main
from services.bot_gateway.app.main import app as bot_app

_OPERATOR = "@calendar_op"
_PROJECT_ID = 11
_CONSENT_URL = "https://accounts.google.test/o/oauth2/auth?state=secret-state-token"


class _StubHitlRepo:
    def get_runtime_config(self, key: str):
        return None

    def set_runtime_config(self, **kwargs):
        pass

    def list_all(self):
        return []


@pytest.fixture
def isolated_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(
        bot_main.settings, "persistence_db_path", str(tmp_path / "story.db")
    )
    monkeypatch.setattr(
        bot_main.settings, "hitl_ticket_db_path", str(tmp_path / "hitl.db")
    )
    monkeypatch.setattr(bot_main.settings, "telegram_bot_token", "TKN")
    monkeypatch.setattr(bot_main.settings, "internal_service_token", "svc-token")
    monkeypatch.setattr(bot_main, "hitl_ticket_repository", _StubHitlRepo())

    sent_dms: list[tuple[int, str]] = []

    async def fake_send_dm(chat_id: int, text: str) -> None:
        sent_dms.append((chat_id, text))

    monkeypatch.setattr(bot_main, "_send_dm", fake_send_dm)
    return {"tmp_path": tmp_path, "dms": sent_dms}


def _message(*, text: str, username: str = "calendar_op", chat_id: int = 100):
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": chat_id},
            "from": {"id": 200, "username": username},
            "text": text,
        },
    }


def _stub_operator_lookup(monkeypatch, *, record):
    calls: list[str] = []

    async def fake_lookup(*, username: str):
        calls.append(username)
        return record

    monkeypatch.setattr(bot_main.api_client, "find_operator_by_username", fake_lookup)
    return calls


def _registered_operator_record():
    return {
        "username": _OPERATOR,
        "chat_id": 100,
        "project_id": _PROJECT_ID,
        "is_active": True,
    }


def test_connect_calendar_designated_operator_dms_consent_url(
    isolated_bot, monkeypatch
):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())
    captured: list[dict] = []

    async def fake_initiate(*, project_id, operator, internal_token):
        captured.append(
            {
                "project_id": project_id,
                "operator": operator,
                "internal_token": internal_token,
            }
        )
        return {"consent_url": _CONSENT_URL}

    monkeypatch.setattr(
        bot_main.api_client, "initiate_calendar_connect", fake_initiate
    )

    client = TestClient(bot_app)
    response = client.post("/telegram/webhook", json=_message(text="/connect_calendar"))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["route"] == "calendar_connect"
    assert body["decision"] == "url_sent"

    assert captured == [
        {
            "project_id": _PROJECT_ID,
            "operator": _OPERATOR,
            "internal_token": "svc-token",
        }
    ]
    assert len(isolated_bot["dms"]) == 1
    chat_id, text = isolated_bot["dms"][0]
    assert chat_id == 100
    assert _CONSENT_URL in text


def test_connect_calendar_is_case_insensitive(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    async def fake_initiate(*, project_id, operator, internal_token):
        return {"consent_url": _CONSENT_URL}

    monkeypatch.setattr(
        bot_main.api_client, "initiate_calendar_connect", fake_initiate
    )

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook", json=_message(text="/Connect_Calendar")
    )
    assert response.json()["decision"] == "url_sent"
    assert len(isolated_bot["dms"]) == 1


def test_connect_calendar_with_trailing_tokens_still_matches(
    isolated_bot, monkeypatch
):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    async def fake_initiate(*, project_id, operator, internal_token):
        return {"consent_url": _CONSENT_URL}

    monkeypatch.setattr(
        bot_main.api_client, "initiate_calendar_connect", fake_initiate
    )

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook", json=_message(text="/connect_calendar please")
    )
    assert response.json()["decision"] == "url_sent"


def test_connect_calendar_non_designated_operator_ignored(
    isolated_bot, monkeypatch, caplog
):
    # Not a registered operator: lookup returns None.
    _stub_operator_lookup(monkeypatch, record=None)

    called = False

    async def fake_initiate(*, project_id, operator, internal_token):
        nonlocal called
        called = True
        return {"consent_url": _CONSENT_URL}

    monkeypatch.setattr(
        bot_main.api_client, "initiate_calendar_connect", fake_initiate
    )

    client = TestClient(bot_app)
    with caplog.at_level(logging.WARNING):
        response = client.post(
            "/telegram/webhook",
            json=_message(text="/connect_calendar", username="random_user"),
        )

    body = response.json()
    assert body["status"] == "ignored"
    assert body["reason"] == "unauthorized_calendar"
    assert called is False
    assert len(isolated_bot["dms"]) == 0
    assert any(
        record.message == "calendar_command_unauthorized"
        for record in caplog.records
    )


def test_connect_calendar_operator_without_project_ignored(
    isolated_bot, monkeypatch
):
    # Registered + active but no project binding: stub the resolver to return
    # a project-less operator so the bot-side gate treats it as unauthorized.
    async def fake_resolve(*, username, api_client):
        from services.bot_gateway.app.operator_resolver import ResolvedOperator

        return ResolvedOperator(
            username=_OPERATOR,
            chat_id=100,
            project_id=None,
            is_active=True,
            source="registry",
        )

    monkeypatch.setattr(
        "services.bot_gateway.app.calendar_commands.resolve_operator_for_sender",
        fake_resolve,
    )

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook", json=_message(text="/connect_calendar")
    )
    body = response.json()
    assert body["status"] == "ignored"
    assert body["reason"] == "unauthorized_calendar"
    assert len(isolated_bot["dms"]) == 0


def test_connect_calendar_api_error_dms_fallback(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    async def fake_initiate(*, project_id, operator, internal_token):
        request = httpx.Request("POST", "http://api/calendar/connect/initiate")
        response = httpx.Response(400, request=request)
        raise httpx.HTTPStatusError("bad", request=request, response=response)

    monkeypatch.setattr(
        bot_main.api_client, "initiate_calendar_connect", fake_initiate
    )

    client = TestClient(bot_app)
    response = client.post("/telegram/webhook", json=_message(text="/connect_calendar"))
    body = response.json()
    assert body["decision"] == "api_error"
    assert len(isolated_bot["dms"]) == 1
    _, text = isolated_bot["dms"][0]
    assert "Не получилось начать" in text
    assert _CONSENT_URL not in text


def test_connect_calendar_missing_url_dms_fallback(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    async def fake_initiate(*, project_id, operator, internal_token):
        return {}

    monkeypatch.setattr(
        bot_main.api_client, "initiate_calendar_connect", fake_initiate
    )

    client = TestClient(bot_app)
    response = client.post("/telegram/webhook", json=_message(text="/connect_calendar"))
    body = response.json()
    assert body["decision"] == "no_url"
    assert len(isolated_bot["dms"]) == 1
    _, text = isolated_bot["dms"][0]
    assert "Не получилось начать" in text


def test_disconnect_calendar_designated_operator_confirms(
    isolated_bot, monkeypatch
):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())
    captured: list[dict] = []

    async def fake_disconnect(*, project_id, operator, internal_token):
        captured.append(
            {
                "project_id": project_id,
                "operator": operator,
                "internal_token": internal_token,
            }
        )
        return {"disconnected": True}

    monkeypatch.setattr(bot_main.api_client, "disconnect_calendar", fake_disconnect)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook", json=_message(text="/disconnect_calendar")
    )
    body = response.json()
    assert body["route"] == "calendar_disconnect"
    assert body["decision"] == "disconnected"
    assert captured == [
        {
            "project_id": _PROJECT_ID,
            "operator": _OPERATOR,
            "internal_token": "svc-token",
        }
    ]
    assert len(isolated_bot["dms"]) == 1
    _, text = isolated_bot["dms"][0]
    assert "Календарь отключён" in text


def test_disconnect_calendar_is_case_insensitive(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    async def fake_disconnect(*, project_id, operator, internal_token):
        return {"disconnected": True}

    monkeypatch.setattr(bot_main.api_client, "disconnect_calendar", fake_disconnect)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook", json=_message(text="/DISCONNECT_CALENDAR")
    )
    assert response.json()["decision"] == "disconnected"


def test_disconnect_calendar_non_designated_operator_ignored(
    isolated_bot, monkeypatch
):
    _stub_operator_lookup(monkeypatch, record=None)

    called = False

    async def fake_disconnect(*, project_id, operator, internal_token):
        nonlocal called
        called = True
        return {"disconnected": True}

    monkeypatch.setattr(bot_main.api_client, "disconnect_calendar", fake_disconnect)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_message(text="/disconnect_calendar", username="random_user"),
    )
    body = response.json()
    assert body["status"] == "ignored"
    assert body["reason"] == "unauthorized_calendar"
    assert called is False
    assert len(isolated_bot["dms"]) == 0


def test_disconnect_calendar_api_error_dms_fallback(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    async def fake_disconnect(*, project_id, operator, internal_token):
        raise httpx.RequestError("boom")

    monkeypatch.setattr(bot_main.api_client, "disconnect_calendar", fake_disconnect)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook", json=_message(text="/disconnect_calendar")
    )
    body = response.json()
    assert body["decision"] == "api_error"
    assert len(isolated_bot["dms"]) == 1
    _, text = isolated_bot["dms"][0]
    assert "Не получилось отключить" in text


def test_non_calendar_command_falls_through(isolated_bot, monkeypatch):
    forwarded: list[dict] = []

    async def fake_forward(**kwargs):
        forwarded.append(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(bot_main.api_client, "forward_inbound", fake_forward)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_message(text="привет", username="customer"),
    )
    assert response.json()["status"] == "accepted"
    assert len(forwarded) == 1
    assert len(isolated_bot["dms"]) == 0
