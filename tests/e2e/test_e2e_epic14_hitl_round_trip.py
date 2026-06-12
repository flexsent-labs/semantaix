"""Epic 14 — HITL event recording end-to-end smoke tests.

Story 14.04: every HITL lifecycle transition writes a usage_hitl_events row.
NFR-8 verified: recorder failure does NOT block the HITL state machine.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.epic("14"),
    pytest.mark.story("14-04"),
]

_CHAT_ID = 14_041
_OPERATOR = "@e2e_hitl_op"


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
    monkeypatch.setattr(api_main, "_resolve_inbound_project_id", lambda chat_id: 1)
    monkeypatch.setattr(api_main, "_default_project_id", lambda: 1)

    usage_db = str(tmp_path / "usage.sqlite3")
    bootstrap_usage_db(usage_db)
    real_recorder = UsageRecorder(
        llm_repo=UsageLlmCallRepository(db_path=usage_db),
        message_repo=UsageMessageRepository(db_path=usage_db),
        hitl_repo=UsageHitlEventRepository(db_path=usage_db),
    )
    monkeypatch.setattr(api_main, "usage_recorder", real_recorder)

    yield {"usage_db": usage_db}


def test_hitl_escalation_records_created_and_assigned(env):
    """Customer message escalation writes created + assigned events."""
    usage_db = env["usage_db"]

    with TestClient(api_app) as client:
        inbound = client.post(
            "/conversations/inbound",
            json={
                "text": "Need help please.",
                "chat_id": _CHAT_ID,
                "trace_id": "t-e2e-14-04-a",
            },
        )
        assert inbound.status_code == 200
        assert inbound.json().get("escalated") is True

    conn = sqlite3.connect(usage_db)
    event_types = [
        r[0]
        for r in conn.execute(
            "SELECT event_type FROM usage_hitl_events ORDER BY id"
        ).fetchall()
    ]
    conn.close()

    assert "created" in event_types
    assert "assigned" in event_types


def test_operator_reply_records_replied_and_resolved(env):
    """Operator reply records replied then resolved events in order."""
    usage_db = env["usage_db"]

    with TestClient(api_app) as client:
        inbound = client.post(
            "/conversations/inbound",
            json={
                "text": "Need help please.",
                "chat_id": _CHAT_ID,
                "trace_id": "t-e2e-14-04-b",
            },
        )
        ticket_id = inbound.json()["hitl_ticket_id"]

        reply = client.post(
            f"/hitl/tickets/{ticket_id}/reply",
            json={"operator_username": _OPERATOR, "reply_text": "Done."},
        )
        assert reply.status_code == 200
        assert reply.json()["resolved"] is True

    conn = sqlite3.connect(usage_db)
    event_types = [
        r[0]
        for r in conn.execute(
            "SELECT event_type FROM usage_hitl_events ORDER BY id"
        ).fetchall()
    ]
    conn.close()

    assert event_types == ["created", "assigned", "replied", "resolved"]


def test_recorder_failure_does_not_block_hitl_state_machine(env, monkeypatch):
    """NFR-8: recorder errors are swallowed; the HITL ticket still resolves."""
    broken_recorder = MagicMock(spec=UsageRecorder)
    broken_recorder.record = AsyncMock(side_effect=RuntimeError("recorder broken"))
    broken_recorder.start = MagicMock()
    broken_recorder.aclose = AsyncMock()
    monkeypatch.setattr(api_main, "usage_recorder", broken_recorder)

    with TestClient(api_app) as client:
        inbound = client.post(
            "/conversations/inbound",
            json={"text": "help", "chat_id": _CHAT_ID, "trace_id": "t-e2e-14-04-c"},
        )
        assert inbound.status_code == 200
        ticket_id = inbound.json()["hitl_ticket_id"]

        reply = client.post(
            f"/hitl/tickets/{ticket_id}/reply",
            json={"operator_username": _OPERATOR, "reply_text": "Sorted."},
        )
        # Reply must succeed even though the recorder is broken (NFR-8)
        assert reply.status_code == 200
        assert reply.json()["resolved"] is True
