"""End-to-end coverage of bot prompt command + pending-edit dispatch wiring
through the /telegram/webhook endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from platform_common.settings import get_settings
from services.bot_gateway.app import main as bot_main
from services.bot_gateway.app.main import api_client, hitl_ticket_repository
from services.bot_gateway.app.main import app as bot_app


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    monkeypatch.setenv("PERSISTENCE_DB_PATH", str(tmp_path / "persistence.sqlite3"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _payload(text: str, *, update_id: int = 5001) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": 1, "username": "alice"},
            "chat": {"id": 1, "type": "private"},
            "text": text,
        },
    }


def test_prompt_set_command_routes_to_prompt_handler(monkeypatch):
    monkeypatch.setattr(
        api_client,
        "arm_prompt_pending_edit",
        AsyncMock(return_value={"armed_for": "@alice"}),
    )
    monkeypatch.setattr(
        bot_main, "_send_dm", AsyncMock(return_value={"ok": True})
    )
    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_payload("/prompt_set default verifier_system"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["route"] == "prompt_set_armed"


def test_pending_edit_dispatch_captures_next_message(monkeypatch):
    monkeypatch.setattr(
        api_client,
        "peek_pending_prompt_edit",
        AsyncMock(
            return_value={
                "project_slug": "default",
                "prompt_name": "verifier_system",
            }
        ),
    )
    monkeypatch.setattr(
        api_client,
        "consume_pending_prompt_edit",
        AsyncMock(
            return_value={
                "version": 4,
                "prompt_name": "verifier_system",
                "project_slug": "default",
            }
        ),
    )
    monkeypatch.setattr(
        bot_main, "_send_dm", AsyncMock(return_value={"ok": True})
    )
    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_payload("здесь мой новый промт", update_id=5002),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["route"] == "prompt_pending_consumed"


def test_pending_peek_swallows_network_error(monkeypatch):
    """If peek_pending_prompt_edit raises, we fall through to the normal
    customer-forward path instead of failing the webhook."""
    import httpx

    monkeypatch.setattr(
        api_client,
        "peek_pending_prompt_edit",
        AsyncMock(side_effect=httpx.ConnectError("api down")),
    )
    monkeypatch.setattr(
        api_client, "forward_inbound", AsyncMock(return_value={})
    )
    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook", json=_payload("hello", update_id=5003)
    )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"


def test_operator_branch_runs_when_no_pending_edit(monkeypatch):
    """With peek explicitly mocked to None, the operator reply path runs."""
    monkeypatch.setattr(
        api_client,
        "find_operator_by_username",
        AsyncMock(
            return_value={"username": "@alice", "chat_id": 1, "project_id": 1, "is_active": True}
        ),
    )
    monkeypatch.setattr(
        api_client,
        "peek_pending_prompt_edit",
        AsyncMock(return_value=None),
    )
    deliver = AsyncMock(return_value={"delivered": True})
    monkeypatch.setattr(api_client, "deliver_operator_reply", deliver)
    ticket = hitl_ticket_repository.create(
        conversation_ref="q",
        reason="awaiting_human_response",
        target_chat_id=999,
    )
    hitl_ticket_repository.assign(
        ticket_id=ticket.id, operator_username="@alice"
    )
    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_payload("operator reply text here", update_id=5004),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "operator_reply_delivered"
