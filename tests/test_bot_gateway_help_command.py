from __future__ import annotations

import itertools

import pytest
from fastapi.testclient import TestClient

from services.bot_gateway.app import main as bot_main
from services.bot_gateway.app.main import app as bot_app


class _StubHitlRepo:
    def __init__(self, runtime_override: str | None = None) -> None:
        self._runtime_override = runtime_override

    def get_runtime_config(self, key: str):
        if key == "hitl_primary_operator_username":
            return self._runtime_override
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
    monkeypatch.setattr(bot_main.settings, "hitl_primary_operator_username", "@ajdevy")
    monkeypatch.setattr(bot_main, "hitl_ticket_repository", _StubHitlRepo())

    sent_dms: list[tuple[int, str]] = []

    async def fake_send_dm(chat_id: int, text: str) -> None:
        sent_dms.append((chat_id, text))

    monkeypatch.setattr(bot_main, "_send_dm", fake_send_dm)
    return {"tmp_path": tmp_path, "dms": sent_dms}


_WEBHOOK_UPDATE_IDS = itertools.count(1)


def _message(*, text: str, username: str = "ajdevy", chat_id: int = 100):
    # Unique update_id per delivery so Story 12.31's entry-claim dedup does not
    # collapse two distinct /help posts (settings-op vs runtime-op) in one test.
    # message_id stays fixed so the customer-path persist behaviour is unchanged.
    update_id = next(_WEBHOOK_UPDATE_IDS)
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "chat": {"id": chat_id},
            "from": {"id": 200, "username": username},
            "text": text,
        },
    }


def test_operator_help_returns_dm_with_command_list(isolated_bot):
    client = TestClient(bot_app)
    response = client.post("/telegram/webhook", json=_message(text="/help"))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "help_sent"

    assert len(isolated_bot["dms"]) == 1
    chat_id, text = isolated_bot["dms"][0]
    assert chat_id == 100
    assert "Команды оператора" in text
    assert "📚 База знаний" in text
    assert "/kb_add" in text
    assert "confidential" in text
    assert "добавь в базу" in text
    assert "/persona" in text
    assert "/hitl_config" in text
    assert "💬 Ответ клиенту" in text
    assert "HITL ticket #N" in text
    assert "/file_delete" in text
    assert "/files_delete_all" in text
    assert "confirm" in text


def test_operator_help_is_case_insensitive(isolated_bot):
    client = TestClient(bot_app)
    response = client.post("/telegram/webhook", json=_message(text="/Help"))
    assert response.json()["status"] == "help_sent"
    assert len(isolated_bot["dms"]) == 1


def test_operator_help_with_trailing_tokens_still_matches(isolated_bot):
    client = TestClient(bot_app)
    response = client.post("/telegram/webhook", json=_message(text="/help kb"))
    assert response.json()["status"] == "help_sent"
    assert len(isolated_bot["dms"]) == 1


def test_help_from_non_operator_falls_through_to_forward(isolated_bot, monkeypatch):
    forwarded: list[dict] = []

    async def fake_forward(**kwargs):
        forwarded.append(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(bot_main.api_client, "forward_inbound", fake_forward)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_message(text="/help", username="customer"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert len(isolated_bot["dms"]) == 0
    assert len(forwarded) == 1
    assert forwarded[0]["text"] == "/help"
    assert forwarded[0]["customer_username"] == "@customer"


def test_help_respects_runtime_operator_override(isolated_bot, monkeypatch):
    monkeypatch.setattr(bot_main, "hitl_ticket_repository", _StubHitlRepo("@runtime_op"))

    forwarded: list[dict] = []

    async def fake_forward(**kwargs):
        forwarded.append(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(bot_main.api_client, "forward_inbound", fake_forward)

    client = TestClient(bot_app)

    settings_op_response = client.post(
        "/telegram/webhook",
        json=_message(text="/help", username="ajdevy"),
    )
    assert settings_op_response.json()["status"] == "accepted"
    assert len(isolated_bot["dms"]) == 0
    assert len(forwarded) == 1

    runtime_op_response = client.post(
        "/telegram/webhook",
        json=_message(text="/help", username="runtime_op"),
    )
    assert runtime_op_response.json()["status"] == "help_sent"
    assert len(isolated_bot["dms"]) == 1
