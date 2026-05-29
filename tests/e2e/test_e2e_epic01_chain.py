"""Epic 01: Telegram webhook -> persistence -> /conversations/inbound pipeline."""

import sqlite3
from datetime import UTC, datetime
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
from services.api.app.main import settings as api_settings
from services.bot_gateway.app.main import app as bot_app
from tests.e2e.db_seed import load_telegram_fixture

pytestmark = [pytest.mark.e2e, pytest.mark.epic("01")]


def _wire(tmp_path) -> None:
    rag_repository.db_path = str(tmp_path / "rag.sqlite3")
    incident_repository.db_path = str(tmp_path / "incidents.sqlite3")
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    answer_trace_repository.db_path = str(tmp_path / "answer_traces.sqlite3")


@pytest.mark.story("01-04")
def test_epic01_e2e_webhook_persist_then_inbound_grounded_rag(monkeypatch, tmp_path):
    _wire(tmp_path)
    persistence_path = tmp_path / "persistence.sqlite3"
    monkeypatch.setattr(api_settings, "persistence_db_path", str(persistence_path))

    monkeypatch.setattr(
        answer_pipeline,
        "run",
        AsyncMock(
            return_value=AnswerResult(
                handled=True,
                text="Счёт-фактуры формируются в первый день каждого месяца.",
                response_mode="grounded_rag",
                metadata={
                    "retrieval": [
                        {
                            "chunk_id": "1",
                            "source_ref": "faq-billing",
                            "score": 0.9,
                            "text_snippet": "billing day 1",
                        }
                    ],
                    "answerer": "grounded_rag",
                    "guardrail_score": 0.95,
                },
            )
        ),
    )
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))

    bot_client = TestClient(bot_app)
    fixture = load_telegram_fixture("update_message_text_basic.json")
    # Stamp a current send time so the message is treated as live, not as
    # redelivered offline backlog (the fixture ships a fixed 2024 ``date``).
    fixture["message"]["date"] = int(datetime.now(UTC).timestamp())
    webhook = bot_client.post("/telegram/webhook", json=fixture)
    assert webhook.status_code == 200
    assert webhook.json()["status"] == "accepted"

    with sqlite3.connect(persistence_path) as connection:
        conversations = connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        messages = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert conversations == 1
    assert messages == 1

    api_client = TestClient(api_app)
    ingest = api_client.post(
        "/rag/ingest",
        json={
            "source_id": "faq-billing",
            "text": "Invoices generate on day one each month for active subscriptions.",
        },
    )
    assert ingest.status_code == 200

    inbound = api_client.post(
        "/conversations/inbound",
        json={
            "text": "Когда формируются счёт-фактуры?",
            "chat_id": 12345,
            "trace_id": "trace-epic01",
        },
    )
    assert inbound.status_code == 200
    body = inbound.json()
    assert body["response_mode"] == "grounded_rag"
    assert body["delivered"] is True
    assert body["escalated"] is False


@pytest.mark.story("01-04")
def test_epic01_e2e_inbound_escalates_on_pipeline_failure(monkeypatch, tmp_path):
    _wire(tmp_path)
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))
    monkeypatch.setattr(
        answer_pipeline, "run", AsyncMock(return_value=AnswerResult(handled=False))
    )
    api_client = TestClient(api_app)
    response = api_client.post(
        "/conversations/inbound", json={"text": "Любой вопрос", "chat_id": 1}
    )
    assert response.status_code == 200
    assert response.json()["escalated"] is True
