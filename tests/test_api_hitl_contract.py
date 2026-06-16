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
from tests.conftest import wire_isolated_primary_operator


def _wire(tmp_path):
    wire_isolated_primary_operator(tmp_path)
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    incident_repository.db_path = str(tmp_path / "incidents.sqlite3")
    rag_repository.db_path = str(tmp_path / "rag.sqlite3")
    answer_trace_repository.db_path = str(tmp_path / "answer_traces.sqlite3")


def _force_escalation(monkeypatch):
    monkeypatch.setattr(
        answer_pipeline, "run", AsyncMock(return_value=AnswerResult(handled=False))
    )


@pytest.mark.e2e
@pytest.mark.epic("04")
@pytest.mark.story("04-02")
def test_inbound_escalation_creates_and_assigns_hitl_ticket(tmp_path, monkeypatch):
    _wire(tmp_path)
    _force_escalation(monkeypatch)
    client = TestClient(api_app)

    response = client.post(
        "/conversations/inbound",
        json={"text": "Need escalation for this customer"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["escalated"] is True
    assert payload["hitl_operator_username"] == "@ajdevy"
    assert isinstance(payload["hitl_ticket_id"], int)

    tickets = client.get("/hitl/tickets").json()["items"]
    assert len(tickets) == 1
    assert tickets[0]["status"] == "assigned"
    assert tickets[0]["operator_username"] == "@ajdevy"
    assert tickets[0]["target_chat_id"] is None


def test_hitl_route_missing_operator_emits_incident(tmp_path, monkeypatch):
    _wire(tmp_path)
    import services.api.app.main as api_main
    monkeypatch.setattr(api_main, "_effective_hitl_operator_username", lambda: "")
    client = TestClient(api_app)
    created = hitl_ticket_repository.create(conversation_ref="conv-2", reason="uncertain")

    response = client.post(f"/hitl/tickets/{created.id}/route", json={"operator_username": None})
    assert response.status_code == 503
    assert response.json()["detail"] == "hitl_operator_missing"

    incidents = client.get("/incidents/hitl_delivery_failures").json()["items"]
    assert len(incidents) == 1


def test_hitl_route_and_resolve_endpoints(tmp_path):
    _wire(tmp_path)
    client = TestClient(api_app)
    created = hitl_ticket_repository.create(conversation_ref="conv-3", reason="policy")

    routed = client.post(f"/hitl/tickets/{created.id}/route", json={"operator_username": "@ops"})
    resolved = client.post(f"/hitl/tickets/{created.id}/resolve")
    assert routed.status_code == 200
    assert routed.json()["status"] == "assigned"
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"


@pytest.mark.e2e
@pytest.mark.epic("04")
@pytest.mark.story("04-02-reply")
def test_hitl_reply_delivered_as_bot_authored_and_auto_resolves(tmp_path, monkeypatch):
    _wire(tmp_path)
    client = TestClient(api_app)
    created = hitl_ticket_repository.create(
        conversation_ref="conv-4",
        reason="low_confidence",
        target_chat_id=99887766,
    )
    hitl_ticket_repository.assign(ticket_id=created.id, operator_username="@ops")
    mock_send = AsyncMock(return_value=4242)
    monkeypatch.setattr(telegram_bot_sender, "send_message", mock_send)

    response = client.post(
        f"/hitl/tickets/{created.id}/reply",
        json={"operator_username": "@ops", "reply_text": "Here is the final answer."},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["delivered"] is True
    assert body["resolved"] is True
    assert body["status"] == "resolved"
    mock_send.assert_awaited_once_with(chat_id=99887766, text="Here is the final answer.")

    # Confirm persistent state
    refreshed = hitl_ticket_repository.get(created.id)
    assert refreshed.status == "resolved"
    assert refreshed.resolved_at is not None


def test_hitl_reply_missing_target_chat_id_emits_incident(tmp_path):
    _wire(tmp_path)
    client = TestClient(api_app)
    created = hitl_ticket_repository.create(conversation_ref="conv-5", reason="policy")
    hitl_ticket_repository.assign(ticket_id=created.id, operator_username="@ops")

    response = client.post(
        f"/hitl/tickets/{created.id}/reply",
        json={"operator_username": "@ops", "reply_text": "Answer body"},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "missing_target_chat_id"
    incidents = client.get("/incidents/hitl_delivery_failures").json()["items"]
    assert len(incidents) == 1


def test_hitl_reply_rejects_non_assigned_operator(tmp_path):
    _wire(tmp_path)
    client = TestClient(api_app)
    created = hitl_ticket_repository.create(
        conversation_ref="conv-6",
        reason="policy",
        target_chat_id=111,
    )
    hitl_ticket_repository.assign(ticket_id=created.id, operator_username="@ops")

    response = client.post(
        f"/hitl/tickets/{created.id}/reply",
        json={"operator_username": "@other", "reply_text": "Nope"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "operator_not_assigned"


def test_hitl_reply_rejects_empty_reply(tmp_path):
    _wire(tmp_path)
    client = TestClient(api_app)
    created = hitl_ticket_repository.create(
        conversation_ref="conv-7",
        reason="policy",
        target_chat_id=222,
    )
    hitl_ticket_repository.assign(ticket_id=created.id, operator_username="@ops")

    response = client.post(
        f"/hitl/tickets/{created.id}/reply",
        json={"operator_username": "@ops", "reply_text": "   "},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "empty_reply"


def test_hitl_reply_missing_bot_token_emits_incident(tmp_path, monkeypatch):
    _wire(tmp_path)
    client = TestClient(api_app)
    created = hitl_ticket_repository.create(
        conversation_ref="conv-8",
        reason="policy",
        target_chat_id=333,
    )
    hitl_ticket_repository.assign(ticket_id=created.id, operator_username="@ops")
    monkeypatch.setattr(
        telegram_bot_sender,
        "send_message",
        AsyncMock(side_effect=RuntimeError("missing_bot_token")),
    )

    response = client.post(
        f"/hitl/tickets/{created.id}/reply",
        json={"operator_username": "@ops", "reply_text": "answer"},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "missing_bot_token"
    incidents = client.get("/incidents/hitl_delivery_failures").json()["items"]
    assert len(incidents) == 1


def test_inbound_uses_operator_table_for_hitl_username(tmp_path, monkeypatch):
    _wire(tmp_path)
    import services.api.app.main as api_main
    from services.api.app.operators import OperatorRepository

    operators_db = str(tmp_path / "operators_override.sqlite3")
    fresh_operators = OperatorRepository(operators_db)
    fresh_operators.create(username="@flexsentlabs", project_id=1)
    monkeypatch.setattr(api_main, "operator_repository", fresh_operators)
    _force_escalation(monkeypatch)
    client = TestClient(api_app)

    response = client.post(
        "/conversations/inbound", json={"text": "Need escalation"}
    )
    assert response.status_code == 200
    assert response.json()["hitl_operator_username"] == "@flexsentlabs"


def test_effective_hitl_operator_chat_id_prefers_runtime_config(tmp_path, monkeypatch):
    import services.api.app.main as api_main
    from services.api.app.main import _effective_hitl_operator_chat_id
    from services.api.app.operators import OperatorRepository

    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    fresh_operators = OperatorRepository(str(tmp_path / "operators.sqlite3"))
    fresh_operators.create(username="@ajdevy", project_id=1, chat_id=111)
    monkeypatch.setattr(api_main, "operator_repository", fresh_operators)
    hitl_ticket_repository.set_runtime_config(
        key="telegram_alert_chat_id", value="222", updated_by="@ajdevy"
    )
    assert _effective_hitl_operator_chat_id() == "222"


def test_effective_hitl_operator_chat_id_falls_back_to_operator_table(tmp_path, monkeypatch):
    import services.api.app.main as api_main
    from services.api.app.main import _effective_hitl_operator_chat_id
    from services.api.app.operators import OperatorRepository

    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    fresh_operators = OperatorRepository(str(tmp_path / "operators.sqlite3"))
    fresh_operators.create(username="@ajdevy", project_id=1, chat_id=333)
    monkeypatch.setattr(api_main, "operator_repository", fresh_operators)
    assert _effective_hitl_operator_chat_id() == "333"


def test_effective_hitl_operator_chat_id_none_when_unset(tmp_path, monkeypatch):
    import services.api.app.main as api_main
    from services.api.app.main import _effective_hitl_operator_chat_id
    from services.api.app.operators import OperatorRepository

    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    fresh_operators = OperatorRepository(str(tmp_path / "operators.sqlite3"))
    monkeypatch.setattr(api_main, "operator_repository", fresh_operators)
    assert _effective_hitl_operator_chat_id() is None


@pytest.mark.asyncio
async def test_notify_hitl_operator_summary_skips_when_no_chat_id(tmp_path, monkeypatch):
    import services.api.app.main as api_main
    from services.api.app.main import _notify_hitl_operator_summary

    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    monkeypatch.setattr(api_main, "_effective_hitl_operator_chat_id", lambda: None)
    mock_send = AsyncMock()
    monkeypatch.setattr(telegram_bot_sender, "send_message", mock_send)

    sent = await _notify_hitl_operator_summary(ticket_id=1, summary="x")
    assert sent is False
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_notify_hitl_operator_summary_rejects_non_numeric_chat_id(
    tmp_path, monkeypatch
):
    import services.api.app.main as api_main
    from services.api.app.main import _notify_hitl_operator_summary

    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    monkeypatch.setattr(api_main, "_effective_hitl_operator_chat_id", lambda: "not-a-number")
    mock_send = AsyncMock()
    monkeypatch.setattr(telegram_bot_sender, "send_message", mock_send)

    sent = await _notify_hitl_operator_summary(ticket_id=1, summary="x")
    assert sent is False
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_notify_hitl_operator_summary_returns_false_on_send_failure(
    tmp_path, monkeypatch
):
    import services.api.app.main as api_main
    from services.api.app.main import _notify_hitl_operator_summary

    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    monkeypatch.setattr(api_main, "_effective_hitl_operator_chat_id", lambda: "650934815")
    monkeypatch.setattr(
        telegram_bot_sender,
        "send_message",
        AsyncMock(side_effect=RuntimeError("missing_bot_token")),
    )

    sent = await _notify_hitl_operator_summary(ticket_id=1, summary="x")
    assert sent is False


@pytest.mark.asyncio
async def test_notify_hitl_operator_summary_sends_and_returns_true(tmp_path, monkeypatch):
    import services.api.app.main as api_main
    from services.api.app.main import _notify_hitl_operator_summary

    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    monkeypatch.setattr(api_main, "_effective_hitl_operator_chat_id", lambda: "650934815")
    mock_send = AsyncMock(return_value=999)
    monkeypatch.setattr(telegram_bot_sender, "send_message", mock_send)

    sent = await _notify_hitl_operator_summary(
        ticket_id=42, summary="assigned to @ops"
    )
    assert sent is True
    mock_send.assert_awaited_once_with(
        chat_id=650934815,
        text="HITL ticket #42: assigned to @ops",
    )


@pytest.mark.e2e
@pytest.mark.epic("04")
@pytest.mark.story("04-operator-dm")
def test_inbound_escalation_dms_operator_when_chat_id_configured(tmp_path, monkeypatch):
    _wire(tmp_path)
    import services.api.app.main as api_main
    from services.api.app.operators import OperatorRepository

    operators_db = str(tmp_path / "operators_override.sqlite3")
    fresh_operators = OperatorRepository(operators_db)
    fresh_operators.create(username="@flexsentlabs", project_id=1, chat_id=650934815)
    monkeypatch.setattr(api_main, "operator_repository", fresh_operators)
    _force_escalation(monkeypatch)
    mock_send = AsyncMock(return_value=1234)
    monkeypatch.setattr(telegram_bot_sender, "send_message", mock_send)
    client = TestClient(api_app)

    response = client.post(
        "/conversations/inbound",
        json={
            "text": "Need escalation",
            "chat_id": 9001,
            "customer_username": "@cust",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["escalated"] is True
    # The operator DM is sent in addition to the customer ack. Find the
    # send_message call whose chat_id matches the configured operator chat.
    operator_calls = [
        c for c in mock_send.await_args_list if c.kwargs["chat_id"] == 650934815
    ]
    assert len(operator_calls) == 1
    operator_text = operator_calls[0].kwargs["text"]
    assert "HITL ticket #" in operator_text
    assert "Need escalation" in operator_text
    assert "@cust" in operator_text


def test_inbound_escalation_skips_dm_when_chat_id_missing(tmp_path, monkeypatch):
    _wire(tmp_path)
    import services.api.app.main as api_main
    monkeypatch.setattr(api_main, "_effective_hitl_operator_chat_id", lambda: None)
    _force_escalation(monkeypatch)
    mock_send = AsyncMock(return_value=1)
    monkeypatch.setattr(telegram_bot_sender, "send_message", mock_send)
    client = TestClient(api_app)

    response = client.post(
        "/conversations/inbound",
        json={"text": "Need escalation", "chat_id": 9001},
    )
    assert response.status_code == 200
    # Only the customer ack was sent; no operator DM (chat_id unconfigured).
    chat_ids = {c.kwargs["chat_id"] for c in mock_send.await_args_list}
    assert chat_ids == {9001}


@pytest.mark.e2e
@pytest.mark.epic("04")
@pytest.mark.story("04-operator-dm")
def test_hitl_route_dms_operator_when_chat_id_configured(tmp_path, monkeypatch):
    _wire(tmp_path)
    import services.api.app.main as api_main
    monkeypatch.setattr(api_main, "_effective_hitl_operator_chat_id", lambda: "650934815")
    mock_send = AsyncMock(return_value=4321)
    monkeypatch.setattr(telegram_bot_sender, "send_message", mock_send)
    client = TestClient(api_app)
    created = hitl_ticket_repository.create(conversation_ref="conv-route-dm", reason="policy")

    response = client.post(
        f"/hitl/tickets/{created.id}/route",
        json={"operator_username": "@flexsentlabs"},
    )

    assert response.status_code == 200
    assert response.json()["operator_username"] == "@flexsentlabs"
    mock_send.assert_awaited_once_with(
        chat_id=650934815,
        text=f"HITL ticket #{created.id}: assigned to @flexsentlabs",
    )
