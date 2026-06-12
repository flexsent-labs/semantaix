"""Epic 14 — LLM usage + message volume recording end-to-end smoke tests.

Story 14.02: grounded RAG answer writes a usage_llm_calls row.
Story 14.03: grounded RAG answer also writes a usage_messages row (direction=out).

The real UsageRecorder is started by the FastAPI startup hook (inside
TestClient's event loop) and drained by the shutdown hook when the TestClient
context exits, so all DB rows are committed before we query them.
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
from services.api.app.answerers.grounded_rag import GroundedRagAnswerer
from services.api.app.main import (
    answer_trace_repository,
    hitl_ticket_repository,
    incident_repository,
    openrouter_client,
    rag_repository,
)
from services.api.app.main import app as api_app
from services.api.app.openrouter_client import GroundingVerdict, LlmUsageCapture
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
    pytest.mark.story("14-02"),
    pytest.mark.story("14-03"),
]

_CHAT_ID = 14_200
_GROUNDED_REPLY = "Доставка занимает 2 рабочих дня по всей России."
_CAP = LlmUsageCapture(
    model_name="gpt-4o",
    prompt_tokens=80,
    completion_tokens=20,
    cost_usd=0.002,
    created_at="2026-06-11T12:00:00Z",
)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    rag_repository.db_path = str(tmp_path / "rag.sqlite3")
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    incident_repository.db_path = str(tmp_path / "incidents.sqlite3")
    answer_trace_repository.db_path = str(tmp_path / "traces.sqlite3")

    monkeypatch.setattr(
        api_main.telegram_bot_sender, "send_message", AsyncMock(return_value=1)
    )
    monkeypatch.setattr(
        openrouter_client,
        "answer_grounded",
        AsyncMock(return_value=(_GROUNDED_REPLY, _CAP)),
    )
    monkeypatch.setattr(
        openrouter_client,
        "verify_grounding",
        AsyncMock(return_value=(GroundingVerdict(label="GROUNDED", reason="ok"), _CAP)),
    )

    # Real recorder backed by a temp SQLite DB.  The FastAPI startup hook calls
    # usage_recorder.start() which binds it to TestClient's event loop; the
    # shutdown hook calls aclose() which drains the queue before the loop tears
    # down — so all DB rows are committed before we query them.
    usage_db = str(tmp_path / "usage.sqlite3")
    bootstrap_usage_db(usage_db)
    real_recorder = UsageRecorder(
        llm_repo=UsageLlmCallRepository(db_path=usage_db),
        message_repo=UsageMessageRepository(db_path=usage_db),
        hitl_repo=MagicMock(spec=UsageHitlEventRepository),
    )
    # Patch BEFORE entering TestClient so the startup hook picks up real_recorder.
    monkeypatch.setattr(api_main, "usage_recorder", real_recorder)
    monkeypatch.setattr(openrouter_client, "_recorder", real_recorder)
    for answerer in api_main.answer_pipeline.answerers:
        if isinstance(answerer, GroundedRagAnswerer):
            monkeypatch.setattr(answerer, "_recorder", real_recorder)
            break

    # Ensure project_id resolves to a non-None value so the outbound
    # usage_messages row is recorded (skipped when project_id is None).
    monkeypatch.setattr(
        api_main, "_resolve_inbound_project_id", lambda chat_id: 14
    )

    rag_repository.ingest(
        source_id="delivery-faq",
        text="Доставка занимает 2 рабочих дня по всей России.",
    )
    hitl_ticket_repository.set_runtime_config(
        key="rag_grounding_score_threshold", value="0.2", updated_by="@admin"
    )

    yield {"usage_db": usage_db}


@pytest.mark.asyncio
async def test_grounded_answer_persists_llm_usage_row(env):
    usage_db = env["usage_db"]

    # The TestClient context triggers startup (start()) and shutdown (aclose()),
    # so by the time we exit the `with` block, all queued records are in SQLite.
    with TestClient(api_app) as client:
        resp = client.post(
            "/conversations/inbound",
            json={
                "text": "сколько идёт доставка?",
                "chat_id": _CHAT_ID,
                "trace_id": "t-e2e-14-02",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["response_mode"] == "grounded_rag"

    conn = sqlite3.connect(usage_db)

    llm_rows = conn.execute(
        "SELECT call_outcome, trace_id, model_name FROM usage_llm_calls"
    ).fetchall()
    assert len(llm_rows) >= 1
    assert "customer_visible_answer" in {r[0] for r in llm_rows}

    msg_rows = conn.execute(
        "SELECT direction, participant_role FROM usage_messages"
    ).fetchall()
    assert len(msg_rows) >= 1, "Expected at least one usage_messages row"
    assert ("out", "customer") in set(msg_rows), (
        f"Expected outbound customer row; got {msg_rows}"
    )
