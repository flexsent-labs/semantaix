"""Epic 04: inbound -> escalation -> route -> reply -> auto-resolve."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.api.app.answerers import AnswerResult
from services.api.app.main import (
    answer_pipeline,
    answer_trace_repository,
    hitl_ticket_repository,
    incident_repository,
    rag_repository,
    telegram_bot_sender,
)
from services.api.app.main import app as api_app
from services.bot_gateway.app.main import (
    api_client as bot_api_client,
)
from services.bot_gateway.app.main import app as bot_app
from services.bot_gateway.app.main import (
    hitl_ticket_repository as bot_hitl_repo,
)

pytestmark = [pytest.mark.e2e, pytest.mark.epic("04")]


def _wire(tmp_path, monkeypatch, *, primary_operator: str = "@ajdevy"):
    import services.api.app.main as _api_main
    hitl_path = str(tmp_path / "hitl.sqlite3")
    hitl_ticket_repository.db_path = hitl_path
    bot_hitl_repo.db_path = hitl_path
    incident_repository.db_path = str(tmp_path / "incidents.sqlite3")
    incident_repository.dedup_window_seconds = 300
    rag_repository.db_path = str(tmp_path / "rag.sqlite3")
    answer_trace_repository.db_path = str(tmp_path / "answer_traces.sqlite3")
    monkeypatch.setattr(_api_main, "_effective_hitl_operator_username", lambda: primary_operator)
    monkeypatch.setattr(
        answer_pipeline, "run", AsyncMock(return_value=AnswerResult(handled=False))
    )


@pytest.mark.story("04-01")
def test_epic04_inbound_escalation_then_route_and_resolve(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))
    client = TestClient(api_app)

    inbound = client.post(
        "/conversations/inbound",
        json={"text": "Customer needs an uncertain answer path."},
    )
    assert inbound.status_code == 200
    body = inbound.json()
    assert body["escalated"] is True
    ticket_id = body["hitl_ticket_id"]

    routed = client.post(
        f"/hitl/tickets/{ticket_id}/route",
        json={"operator_username": "@night_ops"},
    )
    assert routed.status_code == 200
    assert routed.json()["operator_username"] == "@night_ops"

    resolved = client.post(f"/hitl/tickets/{ticket_id}/resolve")
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"


@pytest.mark.story("04-02")
def test_epic04_full_inbound_to_operator_reply_chain(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    send_mock = AsyncMock(return_value=12345)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send_mock)
    client = TestClient(api_app)

    inbound = client.post(
        "/conversations/inbound",
        json={"text": "Help me with this question.", "chat_id": 9001},
    ).json()
    ticket_id = inbound["hitl_ticket_id"]

    tickets = client.get("/hitl/tickets").json()["items"]
    assert tickets[0]["target_chat_id"] == 9001

    client.post(
        f"/hitl/tickets/{ticket_id}/route",
        json={"operator_username": "@operator_a"},
    )
    reply = client.post(
        f"/hitl/tickets/{ticket_id}/reply",
        json={"operator_username": "@operator_a", "reply_text": "Here is the answer."},
    )
    assert reply.status_code == 200
    body = reply.json()
    assert body["delivered"] is True
    assert body["resolved"] is True

    # Verify the operator-authored reply was sent to the customer chat
    sent_to_customer = [
        c for c in send_mock.await_args_list
        if c.kwargs["chat_id"] == 9001 and c.kwargs["text"] == "Here is the answer."
    ]
    assert len(sent_to_customer) == 1

    refreshed = hitl_ticket_repository.get(ticket_id)
    assert refreshed.status == "resolved"


@pytest.mark.story("04-01")
def test_epic04_route_without_operator_emits_incident(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, primary_operator="")
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))
    client = TestClient(api_app)

    inbound = client.post(
        "/conversations/inbound", json={"text": "Question."}
    ).json()
    ticket_id = inbound["hitl_ticket_id"]

    response = client.post(f"/hitl/tickets/{ticket_id}/route", json={})
    assert response.status_code == 503
    assert response.json()["detail"] == "hitl_operator_missing"

    incidents = client.get("/incidents/hitl_delivery_failures").json()["items"]
    assert len(incidents) >= 1
    assert any(item["severity"] == "critical" for item in incidents)


@pytest.mark.story("04-02")
def test_epic04_reply_missing_target_chat_id_emits_incident(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    send_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send_mock)
    client = TestClient(api_app)

    inbound = client.post(
        "/conversations/inbound", json={"text": "Question without chat."}
    ).json()
    ticket_id = inbound["hitl_ticket_id"]

    client.post(
        f"/hitl/tickets/{ticket_id}/route",
        json={"operator_username": "@operator_a"},
    )
    response = client.post(
        f"/hitl/tickets/{ticket_id}/reply",
        json={"operator_username": "@operator_a", "reply_text": "Reply"},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "missing_target_chat_id"

    incidents = client.get("/incidents/hitl_delivery_failures").json()["items"]
    assert any(item["severity"] == "critical" for item in incidents)


@pytest.mark.story("04-02")
def test_epic04_reply_rejects_non_assigned_operator(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    send_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send_mock)
    client = TestClient(api_app)

    inbound = client.post(
        "/conversations/inbound", json={"text": "Question.", "chat_id": 42}
    ).json()
    ticket_id = inbound["hitl_ticket_id"]

    client.post(
        f"/hitl/tickets/{ticket_id}/route",
        json={"operator_username": "@operator_a"},
    )
    response = client.post(
        f"/hitl/tickets/{ticket_id}/reply",
        json={"operator_username": "@operator_b", "reply_text": "Reply"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "operator_not_assigned"


@pytest.mark.story("04-runtime-config")
def test_epic04_runtime_config_overrides_default_operator(tmp_path, monkeypatch):
    # /hitl_config now registers the operator via the operators registry (not
    # via runtime_config DB writes) and sets telegram_alert_chat_id.
    import services.bot_gateway.app.main as _bot_main

    monkeypatch.setattr(_bot_main.settings, "hitl_config_admin_username", "@ajdevy")
    monkeypatch.setattr(_bot_main.settings, "admin_telegram_username", "@ajdevy")
    _wire(tmp_path, monkeypatch)
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))
    # Admin is not in the operators registry — that's expected.
    monkeypatch.setattr(
        bot_api_client, "find_operator_by_username", AsyncMock(return_value=None)
    )
    attach_calls: list[dict] = []

    async def fake_attach(*, username, project_id, chat_id=None, display_name=None):
        attach_calls.append({"username": username, "project_id": project_id, "chat_id": chat_id})
        return {}

    monkeypatch.setattr(bot_api_client, "attach_operator", fake_attach)

    bot_client = TestClient(bot_app)
    config_response = bot_client.post(
        "/telegram/webhook",
        json={
            "update_id": 9100,
            "message": {
                "message_id": 1,
                "from": {"id": 1, "username": "ajdevy"},
                "chat": {"id": 1, "type": "private"},
                "text": "/hitl_config @runtime_op 999",
            },
        },
    )
    assert config_response.json()["status"] == "configured"
    # The operator was registered via the API.
    assert attach_calls == [{"username": "@runtime_op", "project_id": 1, "chat_id": 999}]
    # The alert chat ID is stored for outbound DMs to the operator.
    assert bot_hitl_repo.get_runtime_config("telegram_alert_chat_id") == "999"
