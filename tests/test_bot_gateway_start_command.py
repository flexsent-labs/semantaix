from __future__ import annotations

import itertools

import pytest
from fastapi.testclient import TestClient

from services.bot_gateway.app import main as bot_main
from services.bot_gateway.app.main import app as bot_app


class _StubHitlRepo:
    def get_runtime_config(self, key: str):
        return None

    def set_runtime_config(self, **kwargs):
        pass

    def list_all(self):
        return []


@pytest.fixture
def isolated_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_main.settings, "persistence_db_path", str(tmp_path / "story.db"))
    monkeypatch.setattr(bot_main.settings, "hitl_ticket_db_path", str(tmp_path / "hitl.db"))
    monkeypatch.setattr(bot_main.settings, "telegram_bot_token", "TKN")
    monkeypatch.setattr(bot_main.settings, "admin_telegram_username", "@semantaix-admin")
    monkeypatch.setattr(bot_main.settings, "hitl_config_admin_username", "@semantaix-admin")
    monkeypatch.setattr(bot_main, "hitl_ticket_repository", _StubHitlRepo())

    async def _default_lookup(*, username: str):
        if username == "@ajdevy":
            return {"username": "@ajdevy", "chat_id": 100, "project_id": 1, "is_active": True}
        return None

    monkeypatch.setattr(bot_main.api_client, "find_operator_by_username", _default_lookup)

    sent_dms: list[tuple[int, str]] = []

    async def fake_send_dm(chat_id: int, text: str) -> None:
        sent_dms.append((chat_id, text))

    monkeypatch.setattr(bot_main, "_send_dm", fake_send_dm)
    return {"dms": sent_dms}


_WEBHOOK_UPDATE_IDS = itertools.count(1)


def _message(*, text: str, username: str = "new_user", chat_id: int = 100):
    return {
        "update_id": next(_WEBHOOK_UPDATE_IDS),
        "message": {
            "message_id": 1,
            "chat": {"id": chat_id},
            "from": {"id": 200, "username": username},
            "text": text,
        },
    }


def test_start_guest_gets_registration_instructions(isolated_bot, monkeypatch):
    forwarded: list[dict] = []

    async def fake_forward(**kwargs):
        forwarded.append(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(bot_main.api_client, "forward_inbound", fake_forward)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_message(text="/start", username="guest"),
    )

    assert response.status_code == 200
    assert response.json()["route"] == "start_command"
    assert len(isolated_bot["dms"]) == 1
    assert "/register" in isolated_bot["dms"][0][1]
    assert len(forwarded) == 0


def test_start_operator_gets_operator_welcome(isolated_bot):
    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_message(text="/start", username="ajdevy"),
    )

    assert response.json()["route"] == "start_command"
    assert len(isolated_bot["dms"]) == 1
    assert "/connect_calendar" in isolated_bot["dms"][0][1]
