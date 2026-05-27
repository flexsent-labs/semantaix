"""Webhook-level wiring tests for the new operator services NL handler.

Drives the bot_gateway webhook through :class:`TestClient` and asserts:

1. A service-add NL phrase from an authorized operator → handler short-circuits
   the rest of the pipeline (route ``service_add``, expected confirmation DM).
2. A non-service phrase from the configured operator → handler returns None,
   pipeline keeps walking, and the message reaches the operator-reply branch.

The second case is important for keeping the existing operator-reply test
coverage intact: the new handler must not silently swallow operator chatter
that the rest of the pipeline depends on.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from platform_common.settings import get_settings
from services.bot_gateway.app import main as bot_main
from services.bot_gateway.app.main import (
    api_client,
    hitl_ticket_repository,
)
from services.bot_gateway.app.main import app as bot_app


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    persistence_path = tmp_path / "persistence.sqlite3"
    monkeypatch.setenv("PERSISTENCE_DB_PATH", str(persistence_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _webhook_payload(*, text: str, username: str = "operator", reply_to: str | None = None) -> dict:
    msg: dict = {
        "message_id": 99,
        "from": {"id": 1, "username": username},
        "chat": {"id": 1, "type": "private"},
        "text": text,
    }
    if reply_to is not None:
        msg["reply_to_message"] = {"text": reply_to}
    return {"update_id": 8100, "message": msg}


def test_authorized_operator_nl_add_routes_through_new_handler(monkeypatch):
    """The operator types ``"добавь услугу X"`` → LLM classifies → bot routes
    via the new dispatcher, calls ``add_sales_service``, DMs the confirmation."""
    hitl_ticket_repository.set_runtime_config(
        key="hitl_primary_operator_username",
        value="@operator",
        updated_by="@ajdevy",
    )
    find_op = AsyncMock(
        return_value={
            "username": "@operator",
            "chat_id": 1,
            "project_id": 9,
            "is_active": True,
        }
    )
    add_svc = AsyncMock(return_value={"id": 77})
    monkeypatch.setattr(api_client, "find_operator_by_username", find_op)
    monkeypatch.setattr(api_client, "add_sales_service", add_svc)

    sent: list[tuple[int, str]] = []

    async def fake_send_dm(chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    monkeypatch.setattr(bot_main, "_send_dm", fake_send_dm)

    fake_complete = AsyncMock(
        return_value={"action": "add", "name": "тур", "description": None}
    )
    monkeypatch.setattr(
        bot_main.operator_service_nl_openrouter, "complete_json", fake_complete
    )

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_webhook_payload(text="добавь услугу тур"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body.get("route") == "service_add"
    assert body.get("service_id") == "77"
    add_svc.assert_awaited_once()
    assert (1, "Добавлено: тур (id=77)") in sent


def test_operator_chatter_falls_through_to_operator_reply(monkeypatch):
    """When the LLM returns ``action: null`` (operator chatter, not a service
    op), the dispatcher MUST return None so the rest of the inbound pipeline —
    including the operator-reply branch — keeps running unchanged."""
    hitl_ticket_repository.set_runtime_config(
        key="hitl_primary_operator_username",
        value="@operator",
        updated_by="@ajdevy",
    )
    find_op = AsyncMock(
        return_value={
            "username": "@operator",
            "chat_id": 1,
            "project_id": 9,
            "is_active": True,
        }
    )
    deliver = AsyncMock(return_value={"delivered": True, "resolved": True})
    monkeypatch.setattr(api_client, "find_operator_by_username", find_op)
    monkeypatch.setattr(api_client, "forward_inbound", AsyncMock(return_value={}))
    monkeypatch.setattr(api_client, "deliver_operator_reply", deliver)

    async def fake_send_dm(chat_id: int, text: str) -> None:  # pragma: no cover
        pass

    monkeypatch.setattr(bot_main, "_send_dm", fake_send_dm)

    fake_complete = AsyncMock(return_value={"action": None})
    monkeypatch.setattr(
        bot_main.operator_service_nl_openrouter, "complete_json", fake_complete
    )

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_webhook_payload(
            text="В течение 5 рабочих дней.",
            reply_to="HITL ticket #42 | from @customer | Когда возврат?",
        ),
    )
    assert response.status_code == 200
    body = response.json()
    # operator-reply branch must have fired — the entire downstream tail
    # of _process_telegram_update is what restores coverage of the
    # post-NL-handler return branches.
    assert body["status"] == "operator_reply_delivered"
    deliver.assert_awaited_once()


@pytest.mark.asyncio
async def test_operator_reply_branch_runs_when_classifier_returns_null_direct(monkeypatch):
    """Drive ``_process_telegram_update`` directly (no TestClient / httpx /
    BackgroundTasks middle layer) so pytest-cov tracks the operator-reply
    branch lines. Equivalent in behavior to the TestClient version, but
    instrumented for clean line tracing."""
    from fastapi import BackgroundTasks

    hitl_ticket_repository.set_runtime_config(
        key="hitl_primary_operator_username",
        value="@operator",
        updated_by="@ajdevy",
    )
    find_op = AsyncMock(
        return_value={
            "username": "@operator",
            "chat_id": 1,
            "project_id": 9,
            "is_active": True,
        }
    )
    deliver = AsyncMock(return_value={"delivered": True, "resolved": True})
    monkeypatch.setattr(api_client, "find_operator_by_username", find_op)
    monkeypatch.setattr(api_client, "forward_inbound", AsyncMock(return_value={}))
    monkeypatch.setattr(api_client, "deliver_operator_reply", deliver)

    async def fake_send_dm(chat_id: int, text: str) -> None:  # pragma: no cover
        pass

    monkeypatch.setattr(bot_main, "_send_dm", fake_send_dm)
    monkeypatch.setattr(
        bot_main.operator_service_nl_openrouter,
        "complete_json",
        AsyncMock(return_value={"action": None}),
    )

    payload = _webhook_payload(
        text="В течение 5 рабочих дней.",
        reply_to="HITL ticket #42 | from @customer | Когда возврат?",
    )
    result = await bot_main._process_telegram_update(
        payload, "tg-update-direct", BackgroundTasks()
    )
    assert result["status"] == "operator_reply_delivered"
    deliver.assert_awaited_once()


def test_pending_prompt_edit_branch_runs_when_classifier_returns_null(monkeypatch):
    """Mirrors the operator-chatter fall-through: a pending prompt-edit reply
    must still reach the pending_prompt dispatcher when the new NL classifier
    is conservative (returns ``action: null`` for non-service inputs)."""
    from services.bot_gateway.app import prompt_commands

    hitl_ticket_repository.set_runtime_config(
        key="hitl_primary_operator_username",
        value="@operator",
        updated_by="@ajdevy",
    )
    find_op = AsyncMock(
        return_value={
            "username": "@operator",
            "chat_id": 1,
            "project_id": 9,
            "is_active": True,
        }
    )
    monkeypatch.setattr(api_client, "find_operator_by_username", find_op)
    monkeypatch.setattr(api_client, "forward_inbound", AsyncMock(return_value={}))

    async def fake_send_dm(chat_id: int, text: str) -> None:  # pragma: no cover
        pass

    monkeypatch.setattr(bot_main, "_send_dm", fake_send_dm)

    fake_complete = AsyncMock(return_value={"action": None})
    monkeypatch.setattr(
        bot_main.operator_service_nl_openrouter, "complete_json", fake_complete
    )

    # Force the pending-prompt dispatcher to handle the message: return a
    # synthetic non-None payload so the bot wraps it in the standard
    # ``{trace_id, ...}`` response and returns from that branch.
    fake_dispatch = AsyncMock(
        return_value={"status": "ok", "route": "prompt_edit_applied"}
    )
    monkeypatch.setattr(
        prompt_commands, "dispatch_pending_prompt_edit", fake_dispatch
    )
    # The bot_gateway/main.py imported the symbol at module load time, so we
    # need to monkeypatch THAT bound reference too.
    monkeypatch.setattr(
        bot_main, "dispatch_pending_prompt_edit", fake_dispatch
    )

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_webhook_payload(text="любое сообщение от оператора"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body.get("route") == "prompt_edit_applied"
    fake_dispatch.assert_awaited_once()
