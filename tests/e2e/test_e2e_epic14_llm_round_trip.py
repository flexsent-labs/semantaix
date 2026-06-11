"""Epic 14 / Story 14.02 — LLM usage recording end-to-end smoke test.

Verifies that a grounded RAG answer triggers `usage_recorder.record` with
tracker_type="llm" and the expected call_outcome="customer_visible_answer",
using a mock recorder to avoid touching a real DB.
"""

from __future__ import annotations

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
from services.api.app.usage.recorder import UsageRecorder

pytestmark = [pytest.mark.e2e, pytest.mark.epic("14"), pytest.mark.story("14-02")]

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

    # Replace the recorder with a mock so we can inspect record() calls.
    mock_recorder = MagicMock(spec=UsageRecorder)
    mock_recorder.record = AsyncMock()
    mock_recorder.start = MagicMock()
    mock_recorder.aclose = AsyncMock()
    monkeypatch.setattr(api_main, "usage_recorder", mock_recorder)
    monkeypatch.setattr(openrouter_client, "_recorder", mock_recorder)
    # Patch the GroundedRagAnswerer instance inside the pipeline.
    for answerer in api_main.answer_pipeline.answerers:
        if isinstance(answerer, GroundedRagAnswerer):
            monkeypatch.setattr(answerer, "_recorder", mock_recorder)
            break

    rag_repository.ingest(
        source_id="delivery-faq",
        text="Доставка занимает 2 рабочих дня по всей России.",
    )
    hitl_ticket_repository.set_runtime_config(
        key="rag_grounding_score_threshold", value="0.2", updated_by="@admin"
    )

    yield {"recorder": mock_recorder}


@pytest.mark.asyncio
async def test_grounded_answer_records_llm_usage(env):
    recorder = env["recorder"]
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
    # At least one record() call must have tracker_type="llm"
    llm_calls = [
        c for c in recorder.record.call_args_list
        if c.kwargs.get("tracker_type") == "llm"
    ]
    assert len(llm_calls) >= 1
    outcomes = [c.kwargs["payload"]["call_outcome"] for c in llm_calls]
    assert "customer_visible_answer" in outcomes
