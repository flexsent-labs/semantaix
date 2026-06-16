"""Epic 16 — customer DM on operator user account routes via user_gateway."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import services.api.app.main as api_main
from services.api.app.answerers import AnswerResult
from services.api.app.main import (
    answer_pipeline,
    hitl_ticket_repository,
    operator_repository,
    project_repository,
    telegram_bot_sender,
    user_gateway_client,
)
from services.api.app.main import (
    app as api_app,
)

pytestmark = [pytest.mark.e2e, pytest.mark.epic("16")]


def _wire(tmp_path, monkeypatch):
    hitl_path = str(tmp_path / "hitl.sqlite3")
    operators_path = str(tmp_path / "operators.sqlite3")
    projects_path = str(tmp_path / "projects.sqlite3")
    hitl_ticket_repository.db_path = hitl_path
    operator_repository.db_path = operators_path
    project_repository.db_path = projects_path
    operator_repository.init_schema()
    project_repository.init_schema()
    default = project_repository.ensure_default_project()
    operator = operator_repository.create(
        username="@linked_op",
        project_id=default.id,
        chat_id=555001,
    )
    monkeypatch.setattr(api_main, "_effective_hitl_operator_username", lambda: "@linked_op")
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))
    return operator


@pytest.mark.story("16-08")
def test_epic16_inbound_operator_user_delivers_via_user_gateway(tmp_path, monkeypatch):
    operator = _wire(tmp_path, monkeypatch)
    ug_send = AsyncMock(return_value={"delivered": True})
    monkeypatch.setattr(user_gateway_client, "send_message", ug_send)
    monkeypatch.setattr(
        answer_pipeline,
        "run",
        AsyncMock(
            return_value=AnswerResult(
                handled=True,
                text="Ответ с аккаунта оператора",
                response_mode="grounded_rag",
            )
        ),
    )
    client = TestClient(api_app)

    response = client.post(
        "/conversations/inbound",
        json={
            "text": "Сколько стоит маникюр?",
            "chat_id": 777001,
            "delivery_channel": "operator_user",
            "operator_id": operator.id,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["delivered"] is True
    assert body["answer_text"] == "Ответ с аккаунта оператора"
    ug_send.assert_awaited_once_with(
        operator_id=operator.id,
        chat_id=777001,
        text="Ответ с аккаунта оператора",
    )


@pytest.mark.story("16-08")
def test_epic16_hitl_reply_on_operator_user_channel(tmp_path, monkeypatch):
    operator = _wire(tmp_path, monkeypatch)
    ug_send = AsyncMock(return_value={"delivered": True})
    monkeypatch.setattr(user_gateway_client, "send_message", ug_send)
    monkeypatch.setattr(
        answer_pipeline,
        "run",
        AsyncMock(return_value=AnswerResult(handled=False)),
    )
    client = TestClient(api_app)

    inbound = client.post(
        "/conversations/inbound",
        json={
            "text": "Нужна помощь оператора",
            "chat_id": 888002,
            "delivery_channel": "operator_user",
            "operator_id": operator.id,
        },
    )
    ticket_id = inbound.json()["hitl_ticket_id"]

    reply = client.post(
        f"/hitl/tickets/{ticket_id}/reply",
        json={"operator_username": "@linked_op", "reply_text": "Отвечаю лично"},
    )
    assert reply.status_code == 200
    ug_send.assert_awaited()
    last_call = ug_send.await_args_list[-1]
    assert last_call.kwargs == {
        "operator_id": operator.id,
        "chat_id": 888002,
        "text": "Отвечаю лично",
    }
