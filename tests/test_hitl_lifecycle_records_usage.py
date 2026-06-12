"""Integration test: HITL lifecycle events are written to usage_hitl_events — Story 14.04.

Escalates a customer message to HITL then delivers an operator reply;
asserts usage_hitl_events contains rows in order created → assigned → replied → resolved.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.answerers import AnswerResult
from services.api.app.main import (
    answer_pipeline,
    answer_trace_repository,
    hitl_ticket_repository,
    incident_repository,
    telegram_bot_sender,
)
from services.api.app.main import app as api_app
from services.api.app.usage.migrations import bootstrap_usage_db
from services.api.app.usage.recorder import UsageRecorder
from services.api.app.usage.repositories import (
    UsageHitlEventRepository,
    UsageLlmCallRepository,
    UsageMessageRepository,
)

_CHAT_ID = 14_400
_OPERATOR = "@hitl_lifecycle_op"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    incident_repository.db_path = str(tmp_path / "incidents.sqlite3")
    answer_trace_repository.db_path = str(tmp_path / "traces.sqlite3")

    monkeypatch.setattr(
        answer_pipeline, "run", AsyncMock(return_value=AnswerResult(handled=False))
    )
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))
    monkeypatch.setattr(api_main.settings, "hitl_primary_operator_username", _OPERATOR)

    # Ensure project_id resolves to a non-None value on both paths:
    # ctx.project_id (inbound escalation) and _default_project_id() (reply endpoint)
    monkeypatch.setattr(api_main, "_resolve_inbound_project_id", lambda chat_id: 1)
    monkeypatch.setattr(api_main, "_default_project_id", lambda: 1)

    usage_db = str(tmp_path / "usage.sqlite3")
    bootstrap_usage_db(usage_db)
    real_recorder = UsageRecorder(
        llm_repo=UsageLlmCallRepository(db_path=usage_db),
        message_repo=UsageMessageRepository(db_path=usage_db),
        hitl_repo=UsageHitlEventRepository(db_path=usage_db),
    )
    # Patch before entering TestClient so the startup hook picks up real_recorder.
    monkeypatch.setattr(api_main, "usage_recorder", real_recorder)

    yield {"usage_db": usage_db}


def test_hitl_lifecycle_writes_four_events_in_order(env):
    """created → assigned → replied → resolved events are committed in order.

    Uses TestClient's startup/shutdown lifecycle: startup calls recorder.start(),
    shutdown calls aclose() which drains the queue before the with-block exits.
    """
    usage_db = env["usage_db"]

    with TestClient(api_app) as client:
        # 1. Inbound escalation → creates ticket and auto-assigns → "created" + "assigned"
        inbound = client.post(
            "/conversations/inbound",
            json={
                "text": "I need help with something.",
                "chat_id": _CHAT_ID,
                "trace_id": "t-hitl-lifecycle-01",
            },
        )
        assert inbound.status_code == 200
        body = inbound.json()
        assert body.get("escalated") is True
        ticket_id = body["hitl_ticket_id"]

        # 2. Operator reply auto-resolves the ticket → "replied" + "resolved"
        reply = client.post(
            f"/hitl/tickets/{ticket_id}/reply",
            json={"operator_username": _OPERATOR, "reply_text": "Here is your answer."},
        )
        assert reply.status_code == 200
        assert reply.json()["resolved"] is True
    # By the time TestClient exits, aclose() has drained the queue — all rows committed.

    conn = sqlite3.connect(usage_db)
    rows = conn.execute(
        "SELECT event_type, ticket_id, project_id FROM usage_hitl_events ORDER BY id"
    ).fetchall()
    conn.close()

    event_types = [r[0] for r in rows]
    assert event_types == ["created", "assigned", "replied", "resolved"], (
        f"Expected 4 events in order; got {event_types}"
    )

    ticket_ids = {r[1] for r in rows}
    assert ticket_ids == {ticket_id}, f"All rows must carry ticket_id={ticket_id}"

    assert all(r[2] is not None for r in rows), "project_id must be non-None for all rows"
