from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import services.api.app.main as main_mod
from services.api.app.answerers import AnswerResult
from services.api.app.main import (
    answer_pipeline,
    answer_trace_repository,
    hitl_ticket_repository,
    incident_repository,
    rag_repository,
    settings,
    telegram_bot_sender,
)
from services.api.app.main import app as api_app


@pytest.fixture(autouse=True)
def _no_interim(monkeypatch):
    """Suppress interim ack in all tests in this file.

    These tests cover HITL/escalation/dedup flows. The interim message
    is tested separately in test_inbound_interim_ack.py; disabling it here
    keeps call-count assertions stable regardless of which texts happen to
    match sales-intent phrases.
    """
    monkeypatch.setattr(main_mod, "_should_send_interim", lambda text, chat_id: False)


def _wire(tmp_path) -> None:
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    incident_repository.db_path = str(tmp_path / "incidents.sqlite3")
    rag_repository.db_path = str(tmp_path / "rag.sqlite3")
    answer_trace_repository.db_path = str(tmp_path / "answer_traces.sqlite3")


def _stub_pipeline(monkeypatch, result: AnswerResult) -> AsyncMock:
    mock = AsyncMock(return_value=result)
    monkeypatch.setattr(answer_pipeline, "run", mock)
    return mock


@pytest.mark.e2e
@pytest.mark.epic("inbound")
def test_inbound_empty_text_returns_400(tmp_path):
    _wire(tmp_path)
    client = TestClient(api_app)
    response = client.post("/conversations/inbound", json={"text": "   "})
    assert response.status_code == 400
    assert response.json()["detail"] == "empty_text"


def test_inbound_pipeline_handled_delivers_and_persists_trace(tmp_path, monkeypatch):
    _wire(tmp_path)
    send_mock = AsyncMock(return_value=42)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send_mock)
    _stub_pipeline(
        monkeypatch,
        AnswerResult(
            handled=True,
            text="Сейчас 14:32",
            response_mode="deterministic_datetime",
            metadata={"answerer": "datetime"},
        ),
    )
    client = TestClient(api_app)

    response = client.post(
        "/conversations/inbound",
        json={
            "text": "Какое сегодня число?",
            "chat_id": 9001,
            "trace_id": "trace-1",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["delivered"] is True
    assert body["escalated"] is False
    assert body["response_mode"] == "deterministic_datetime"
    assert body["answer_text"] == "Сейчас 14:32"
    assert body["answerer"] == "datetime"
    assert body["trace_id"] == "trace-1"
    send_mock.assert_awaited_once_with(chat_id=9001, text="Сейчас 14:32")

    trace = client.get("/answer-traces/trace-1").json()
    assert trace["response_mode"] == "deterministic_datetime"
    assert trace["guardrail_outcome"] == "valid"


def test_inbound_pipeline_unhandled_escalates_to_hitl(tmp_path, monkeypatch):
    _wire(tmp_path)
    settings.hitl_primary_operator_username = "@ajdevy"
    send_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send_mock)
    _stub_pipeline(monkeypatch, AnswerResult(handled=False))
    client = TestClient(api_app)

    response = client.post(
        "/conversations/inbound",
        json={
            "text": "Когда придёт мой возврат?",
            "chat_id": 9001,
            "customer_username": "@customer",
            "trace_id": "trace-esc",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["delivered"] is False
    assert body["escalated"] is True
    assert body["response_mode"] == "human_only"
    assert body["hitl_operator_username"] == "@ajdevy"
    assert isinstance(body["hitl_ticket_id"], int)

    tickets = client.get("/hitl/tickets").json()["items"]
    assert len(tickets) == 1
    assert tickets[0]["status"] == "assigned"
    assert tickets[0]["operator_username"] == "@ajdevy"
    assert tickets[0]["target_chat_id"] == 9001

    trace = client.get("/answer-traces/trace-esc").json()
    assert trace["response_mode"] == "human_only"
    assert trace["guardrail_outcome"] == "escalated"
    assert "awaiting_human_response" in trace["limitations"]


def test_inbound_escalation_sends_ack_to_customer(tmp_path, monkeypatch):
    _wire(tmp_path)
    send_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send_mock)
    _stub_pipeline(monkeypatch, AnswerResult(handled=False))
    client = TestClient(api_app)

    client.post(
        "/conversations/inbound",
        json={
            "text": "Когда придёт возврат?",
            "chat_id": 9001,
            "customer_username": "@customer",
        },
    )
    chat_ids_sent_to = [call.kwargs["chat_id"] for call in send_mock.await_args_list]
    assert 9001 in chat_ids_sent_to
    ack_call = next(c for c in send_mock.await_args_list if c.kwargs["chat_id"] == 9001)
    assert "уточню" in ack_call.kwargs["text"].lower()
    assert "бот" not in ack_call.kwargs["text"].lower()


def test_inbound_escalation_notifies_operator_with_question(tmp_path, monkeypatch):
    _wire(tmp_path)
    settings.hitl_primary_operator_chat_id = "555"
    send_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send_mock)
    _stub_pipeline(monkeypatch, AnswerResult(handled=False))
    client = TestClient(api_app)

    client.post(
        "/conversations/inbound",
        json={
            "text": "Когда придёт возврат?",
            "chat_id": 9001,
            "customer_username": "@customer",
        },
    )
    operator_calls = [
        c for c in send_mock.await_args_list if c.kwargs["chat_id"] == 555
    ]
    assert len(operator_calls) == 1
    assert "HITL ticket #" in operator_calls[0].kwargs["text"]
    assert "@customer" in operator_calls[0].kwargs["text"]
    assert "Когда придёт возврат?" in operator_calls[0].kwargs["text"]
    settings.hitl_primary_operator_chat_id = None


def test_inbound_ack_failure_emits_incident_but_returns_200(tmp_path, monkeypatch):
    _wire(tmp_path)
    settings.hitl_primary_operator_chat_id = None
    monkeypatch.setattr(
        telegram_bot_sender,
        "send_message",
        AsyncMock(side_effect=RuntimeError("missing_bot_token")),
    )
    _stub_pipeline(monkeypatch, AnswerResult(handled=False))
    client = TestClient(api_app)

    response = client.post(
        "/conversations/inbound",
        json={"text": "Когда придёт возврат?", "chat_id": 9001},
    )
    assert response.status_code == 200
    incidents = client.get("/incidents/hitl_delivery_failures").json()["items"]
    assert len(incidents) >= 1


def test_inbound_uses_runtime_configured_operator(tmp_path, monkeypatch):
    _wire(tmp_path)
    settings.hitl_primary_operator_username = "@default_op"
    hitl_ticket_repository.set_runtime_config(
        key="hitl_primary_operator_username",
        value="@runtime_op",
        updated_by="@ajdevy",
    )
    monkeypatch.setattr(
        telegram_bot_sender, "send_message", AsyncMock(return_value=1)
    )
    _stub_pipeline(monkeypatch, AnswerResult(handled=False))
    client = TestClient(api_app)

    response = client.post(
        "/conversations/inbound", json={"text": "anything"}
    )
    assert response.json()["hitl_operator_username"] == "@runtime_op"


def test_inbound_invalid_grounding_threshold_falls_back_to_setting(tmp_path, monkeypatch):
    _wire(tmp_path)
    hitl_ticket_repository.set_runtime_config(
        key="rag_grounding_score_threshold",
        value="not-a-number",
        updated_by="@admin",
    )
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))
    captured_threshold: list[float] = []

    async def _capture(*, question, ctx):
        captured_threshold.append(ctx.grounding_threshold)
        return AnswerResult(handled=False)

    monkeypatch.setattr(answer_pipeline, "run", _capture)
    client = TestClient(api_app)
    client.post("/conversations/inbound", json={"text": "x", "chat_id": 1})
    # Bad value → falls back to the settings default (0.6)
    assert captured_threshold == [settings.rag_grounding_score_threshold]


def test_inbound_invalid_operator_chat_id_skips_notification(tmp_path, monkeypatch):
    _wire(tmp_path)
    settings.hitl_primary_operator_chat_id = "not-a-number"
    send_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send_mock)
    _stub_pipeline(monkeypatch, AnswerResult(handled=False))
    client = TestClient(api_app)
    client.post(
        "/conversations/inbound", json={"text": "Когда возврат?", "chat_id": 9001}
    )
    # No operator DM sent because the chat_id parse failed silently.
    operator_calls = [
        c for c in send_mock.await_args_list if c.kwargs["chat_id"] != 9001
    ]
    assert operator_calls == []
    settings.hitl_primary_operator_chat_id = None


def test_inbound_replaying_same_trace_id_does_not_resend_ack_or_create_ticket(
    tmp_path, monkeypatch
):
    """Three POSTs with the same trace_id must produce one ack, one HITL
    ticket, one operator DM. This is the api-side defence in depth that
    pairs with the bot_gateway dedup short-circuit — together they make
    triple-ack impossible regardless of which layer the duplicate hits."""
    _wire(tmp_path)
    import services.api.app.main as main_mod
    monkeypatch.setattr(main_mod, "_should_send_interim", lambda text, chat_id: False)
    settings.hitl_primary_operator_username = "@op"
    settings.hitl_primary_operator_chat_id = "555"
    send_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send_mock)
    _stub_pipeline(monkeypatch, AnswerResult(handled=False))
    client = TestClient(api_app)

    payload = {
        "text": "когда могу снять багги?",
        "chat_id": 9001,
        "customer_username": "@customer",
        "trace_id": "tg-update-12345",
    }
    first = client.post("/conversations/inbound", json=payload).json()
    second = client.post("/conversations/inbound", json=payload).json()
    third = client.post("/conversations/inbound", json=payload).json()

    # send_message is called twice on the first request only (ack to
    # customer + operator DM). Replays return cached metadata without any
    # outbound side effects.
    assert send_mock.await_count == 2
    chats_dialed = {call.kwargs["chat_id"] for call in send_mock.await_args_list}
    assert chats_dialed == {9001, 555}

    # All three responses point at the same trace and the same ticket id.
    assert first["hitl_ticket_id"] == second["hitl_ticket_id"] == third["hitl_ticket_id"]
    assert second.get("deduplicated") is True
    assert third.get("deduplicated") is True

    tickets = client.get("/hitl/tickets").json()["items"]
    assert len(tickets) == 1
    settings.hitl_primary_operator_chat_id = None


def test_inbound_inflight_duplicate_is_deduped_without_processing(tmp_path, monkeypatch):
    """Story 12.24 — a retry that arrives while the first request is still
    in-flight (claim taken, trace not yet finalized) must be deduplicated
    BEFORE the pipeline runs, so the customer line is never sent twice.

    Pre-claiming the trace simulates the first (slow) request having claimed
    but not yet written its answer trace — exactly the window the live
    duplicate-send bug exploited.
    """
    _wire(tmp_path)
    send_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send_mock)
    pipeline_mock = _stub_pipeline(
        monkeypatch,
        AnswerResult(handled=True, text="привет", response_mode="x", metadata={}),
    )
    # First request claimed this trace_id and is still processing (no finalized
    # answer_trace row yet) — find_by_trace_id would miss, so the claim is the
    # only guard.
    assert answer_trace_repository.claim_inbound("tg-update-inflight") is True
    client = TestClient(api_app)

    body = client.post(
        "/conversations/inbound",
        json={
            "text": "хочу забронировать багги",
            "chat_id": 9100,
            "trace_id": "tg-update-inflight",
        },
    ).json()

    assert body.get("deduplicated") is True
    assert body["delivered"] is False
    assert body["trace_id"] == "tg-update-inflight"
    # Short-circuited before any side effects: no pipeline run, no send.
    pipeline_mock.assert_not_awaited()
    send_mock.assert_not_awaited()


def test_inbound_followup_question_coalesces_to_active_ticket(tmp_path, monkeypatch):
    """A customer's second unhandled question (different trace_id) while
    they already have an active ticket must NOT spawn a second ticket and
    must NOT re-ack the customer. The operator gets a follow-up DM tagged
    with the existing ticket id."""
    _wire(tmp_path)
    import services.api.app.main as main_mod
    monkeypatch.setattr(main_mod, "_should_send_interim", lambda text, chat_id: False)
    settings.hitl_primary_operator_username = "@op"
    settings.hitl_primary_operator_chat_id = "555"
    send_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send_mock)
    _stub_pipeline(monkeypatch, AnswerResult(handled=False))
    client = TestClient(api_app)

    first = client.post(
        "/conversations/inbound",
        json={
            "text": "когда могу снять багги?",
            "chat_id": 9001,
            "customer_username": "@customer",
            "trace_id": "tg-update-1",
        },
    ).json()
    second = client.post(
        "/conversations/inbound",
        json={
            "text": "алло?",
            "chat_id": 9001,
            "customer_username": "@customer",
            "trace_id": "tg-update-2",
        },
    ).json()

    assert first["hitl_ticket_id"] == second["hitl_ticket_id"]
    assert second.get("coalesced") is True

    # Acks: only the first message gets one. The customer should not see
    # the "Минутку, уточню..." line twice.
    customer_calls = [
        c for c in send_mock.await_args_list if c.kwargs["chat_id"] == 9001
    ]
    assert len(customer_calls) == 1
    assert "уточню" in customer_calls[0].kwargs["text"].lower()

    # Operator gets two DMs (original + follow-up) but they share the
    # same HITL ticket id so the operator sees one conversation.
    operator_calls = [
        c for c in send_mock.await_args_list if c.kwargs["chat_id"] == 555
    ]
    assert len(operator_calls) == 2
    ticket_id = first["hitl_ticket_id"]
    assert all(f"HITL ticket #{ticket_id}" in c.kwargs["text"] for c in operator_calls)
    assert "[follow-up]" in operator_calls[1].kwargs["text"]

    # And of course exactly one HITL ticket exists.
    tickets = client.get("/hitl/tickets").json()["items"]
    assert len(tickets) == 1
    settings.hitl_primary_operator_chat_id = None


def test_inbound_project_prompt_override_wins_over_runtime_config(
    tmp_path, monkeypatch
):
    """Per-project override takes precedence over the global runtime_config."""
    _wire(tmp_path)
    # Point the project prompt repo at an isolated DB and seed an override
    # for the default project so the resolution helper finds it.
    from services.api.app import main as api_main
    from services.api.app.project_prompts import ProjectPromptRepository

    project_prompts = ProjectPromptRepository(
        str(tmp_path / "project_prompts.sqlite3")
    )
    monkeypatch.setattr(api_main, "project_prompt_repository", project_prompts)
    default_project = api_main.project_repository.ensure_default_project()
    project_prompts.set(
        project_id=default_project.id,
        prompt_name="inbound_ack",
        value="per-project ack wins",
        edited_by="@admin",
    )
    # Global runtime_config is also set — to prove the per-project value wins.
    hitl_ticket_repository.set_runtime_config(
        key="inbound_ack_message",
        value="runtime ack (should not be used)",
        updated_by="@admin",
    )
    send_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send_mock)
    _stub_pipeline(monkeypatch, AnswerResult(handled=False))
    client = TestClient(api_app)

    client.post(
        "/conversations/inbound", json={"text": "anything", "chat_id": 9002}
    )
    ack_call = next(
        c for c in send_mock.await_args_list if c.kwargs["chat_id"] == 9002
    )
    assert ack_call.kwargs["text"] == "per-project ack wins"


def test_inbound_runtime_config_overrides_ack_message(tmp_path, monkeypatch):
    _wire(tmp_path)
    hitl_ticket_repository.set_runtime_config(
        key="inbound_ack_message",
        value="custom ack",
        updated_by="@admin",
    )
    send_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send_mock)
    _stub_pipeline(monkeypatch, AnswerResult(handled=False))
    client = TestClient(api_app)

    client.post(
        "/conversations/inbound", json={"text": "anything", "chat_id": 9001}
    )
    ack_call = next(c for c in send_mock.await_args_list if c.kwargs["chat_id"] == 9001)
    assert ack_call.kwargs["text"] == "custom ack"
