"""Ensure the admin project command dispatcher is wired into the webhook chain."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.bot_gateway.app import main as bot_main
from services.bot_gateway.app.main import app as bot_app


class _StubHitlRepo:
    def get_runtime_config(self, key: str):
        return None

    def set_runtime_config(self, **_kwargs):
        pass


@pytest.fixture
def wired_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(
        bot_main.settings, "persistence_db_path", str(tmp_path / "story.db")
    )
    monkeypatch.setattr(
        bot_main.settings, "hitl_ticket_db_path", str(tmp_path / "hitl.db")
    )
    monkeypatch.setattr(bot_main.settings, "telegram_bot_token", "TKN")
    monkeypatch.setattr(bot_main.settings, "admin_telegram_username", "@admin")
    monkeypatch.setattr(bot_main, "hitl_ticket_repository", _StubHitlRepo())

    async def fake_send_dm(chat_id, text):
        return None

    monkeypatch.setattr(bot_main, "_send_dm", fake_send_dm)

    list_projects = AsyncMock(
        return_value={"items": [{"id": 1, "slug": "default", "name": "Default"}]}
    )
    monkeypatch.setattr(bot_main.api_client, "list_projects", list_projects)
    return tmp_path


def _admin_message(text: str) -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": 1},
            "from": {"id": 1, "username": "admin"},
            "text": text,
        },
    }


def test_webhook_routes_admin_project_command(wired_bot):
    client = TestClient(bot_app)
    response = client.post("/telegram/webhook", json=_admin_message("/projects"))
    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "projects_list"
    assert "trace_id" in body


def test_webhook_routes_admin_nl_dialog(wired_bot, monkeypatch):
    propose = AsyncMock(
        return_value={
            "id": 7,
            "status": "pending_confirmation",
            "confirm_token": "tok",
            "preview": "Создать проект…",
            "op_type": "project_create",
        }
    )
    monkeypatch.setattr(bot_main.api_client, "admin_nl_ops_propose", propose)
    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_admin_message("создай проект billing Биллинг команда"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "admin_nl_propose"
    assert body["session_id"] == "7"


def _operator_message(text: str, username: str = "op") -> dict:
    return {
        "update_id": 2,
        "message": {
            "message_id": 2,
            "chat": {"id": 42},
            "from": {"id": 42, "username": username},
            "text": text,
        },
    }


def test_webhook_routes_services_nl_dialog_propose(wired_bot, monkeypatch):
    """Services NL dispatcher is wired BEFORE customer fall-through."""
    from services.bot_gateway.app import services_nl_dialog

    services_nl_dialog._reset_token_cache_for_tests()
    monkeypatch.setattr(
        bot_main,
        "handle_operator_service_nl_message",
        AsyncMock(return_value=None),
    )

    find_op = AsyncMock(
        return_value={
            "username": "@op",
            "chat_id": 42,
            "project_id": 1,
            "is_active": True,
        }
    )
    monkeypatch.setattr(
        bot_main.api_client, "find_operator_by_username", find_op
    )
    propose = AsyncMock(
        return_value={
            "session_id": 11,
            "status": "pending_confirmation",
            "preview": "Создать услугу «маникюр».",
            "confirm_token": "tok-xyz",
            "op_type": "service_add",
        }
    )
    monkeypatch.setattr(
        bot_main.api_client, "services_nl_propose", propose
    )
    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_message("добавь услугу маникюр на 60 минут"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "services_nl_propose"
    assert body["session_id"] == "11"
    propose.assert_awaited_once()


def test_webhook_services_nl_unauthorized_is_routed(wired_bot, monkeypatch):
    """Services NL trigger from a non-registered sender returns the
    unauthorized_services reason — main.py treats it as handled so the
    customer-message fall-through never sees the trigger phrase."""
    from services.bot_gateway.app import services_nl_dialog

    services_nl_dialog._reset_token_cache_for_tests()

    find_op = AsyncMock(return_value=None)
    monkeypatch.setattr(
        bot_main.api_client, "find_operator_by_username", find_op
    )
    propose = AsyncMock()
    monkeypatch.setattr(
        bot_main.api_client, "services_nl_propose", propose
    )
    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_message("добавь услугу маникюр", username="stranger"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reason"] == "unauthorized_services"
    propose.assert_not_awaited()
