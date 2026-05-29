"""PDF ingest -> retrieve -> grounded answer round trip.

Regression coverage for the bug where the live bot escalated
"хочу поехать на багги тур" to a human even though the buggy-tour
brochure Презентация 26.pdf was the operator's intended answer source:

1. The brochure was never ingested (operator workflow ran without
   uploading), so rag_chunks was empty and GroundedRagAnswerer fell
   through with reason="no_chunks".
2. Even after ingest, the brochure's slide-style layout made pypdf
   emit "ТУРЫ НА ЭНДУРО,\\nКВАДРОЦИКЛАХ,\\nБАГГИ В СОЧИ" as three
   separate lines, the RAG chunker line-split them into three chunks,
   and no single chunk had BOTH "багги" AND "тур", capping the top
   retrieval score at 0.5 — below the default 0.6 grounding threshold.

The chunking half is fixed in extractors._unwrap_paragraphs; this test
asserts the full flow round-trips against the actual PDF fixture.
"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.api.app.main import (
    answer_trace_repository,
    hitl_ticket_repository,
    incident_repository,
    knowledge_moderation_repository,
    openrouter_client,
    rag_repository,
    telegram_bot_sender,
)
from services.api.app.main import app as api_app
from services.api.app.openrouter_client import GroundingVerdict

pytestmark = [pytest.mark.e2e]

_FIXTURE_PDF = Path(__file__).parent / "fixtures" / "kozlotur_brochure_26.pdf"
_BUGGY_TOUR_QUERY = "хочу поехать на багги тур"
_GROUNDED_REPLY = (
    "Да, у нас есть туры на багги в Сочи и Красной Поляне — "
    "Yamaha Viking 700 для семейных поездок и BRP Sport Trail 1000 для экстрима."
)


def _wire(tmp_path, monkeypatch):
    """Point every repository singleton at tmp_path and stub the LLM + Telegram.

    The LLM stub mirrors the pattern in tests/e2e/test_e2e_epic08_answer_trace.py
    (AsyncMock on openrouter_client). We use the real AnswerPipeline so the
    extract -> chunk -> retrieve -> grounded answer path is fully exercised.
    """
    rag_repository.db_path = str(tmp_path / "rag.sqlite3")
    knowledge_moderation_repository.db_path = str(tmp_path / "knowledge.sqlite3")
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    incident_repository.db_path = str(tmp_path / "incidents.sqlite3")
    answer_trace_repository.db_path = str(tmp_path / "answer_traces.sqlite3")

    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))
    monkeypatch.setattr(
        openrouter_client,
        "answer_grounded",
        AsyncMock(return_value=_GROUNDED_REPLY),
    )
    monkeypatch.setattr(
        openrouter_client,
        "verify_grounding",
        AsyncMock(
            return_value=GroundingVerdict(
                label="GROUNDED",
                reason="brochure mentions буровые туры in Sochi",
            )
        ),
    )


def _ingest_brochure(client: TestClient) -> int:
    """POST the fixture PDF through /knowledge/operator_upload, return candidate_id."""
    response = client.post(
        "/knowledge/operator_upload",
        json={
            "operator_username": "@flexsentlabs",
            "source_file_type": "pdf",
            "source_file_name": "kozlotur_brochure_26.pdf",
            "stored_binary_path": str(_FIXTURE_PDF),
            "is_confidential": False,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deduplicated"] is False
    assert body["inserted_chunks"] > 0, body
    return int(body["candidate_id"])


def test_pdf_ingest_makes_buggy_tour_query_grounded(tmp_path, monkeypatch):
    """End-to-end: upload the brochure, ask the user's exact question,
    assert the bot answers from RAG instead of escalating to a human."""
    _wire(tmp_path, monkeypatch)
    client = TestClient(api_app)

    candidate_id = _ingest_brochure(client)
    source_id = f"knowledge_candidate:{candidate_id}"

    # Retrieval must surface the brochure with a score that clears the
    # default 0.6 grounding threshold. The _unwrap_paragraphs fix lets a
    # single chunk contain both "багги" and "туры" so the score reaches 1.0.
    retrieve = client.post(
        "/rag/retrieve", json={"query": _BUGGY_TOUR_QUERY, "limit": 3}
    )
    assert retrieve.status_code == 200
    items = retrieve.json()["items"]
    assert items, "retrieval returned no chunks — brochure is not indexed"
    assert items[0]["source_id"] == source_id
    assert items[0]["score"] >= 0.6, items

    # Full pipeline through /conversations/inbound must return a grounded
    # answer, not the escalation ack.
    inbound = client.post(
        "/conversations/inbound",
        json={
            "text": _BUGGY_TOUR_QUERY,
            "chat_id": 999001,
            "customer_username": "test_customer",
            "trace_id": "e2e-pdf-buggy",
        },
    )
    assert inbound.status_code == 200, inbound.text
    body = inbound.json()
    assert body["escalated"] is False, body
    assert body["response_mode"] == "grounded_rag", body
    assert body["answerer"] == "grounded_rag", body
    assert body["answer_text"] == _GROUNDED_REPLY
    assert body["trace_id"] == "e2e-pdf-buggy"


def test_pdf_ingest_dedupes_on_sha256(tmp_path, monkeypatch):
    """Second upload of the same file short-circuits on binary_sha256."""
    _wire(tmp_path, monkeypatch)
    client = TestClient(api_app)

    first = _ingest_brochure(client)

    second = client.post(
        "/knowledge/operator_upload",
        json={
            "operator_username": "@flexsentlabs",
            "source_file_type": "pdf",
            "source_file_name": "kozlotur_brochure_26_again.pdf",
            "stored_binary_path": str(_FIXTURE_PDF),
            "is_confidential": False,
        },
    )
    assert second.status_code == 200
    body = second.json()
    assert body["deduplicated"] is True
    assert body["inserted_chunks"] == 0
    assert body["candidate_id"] == first


def test_buggy_tour_query_without_ingest_declines(tmp_path, monkeypatch):
    """Lock in the empty-KB contract: no chunks -> scope guard polite decline.

    Catches a future change that lowers the grounding threshold to 0 or
    starts answering ungrounded.
    """
    _wire(tmp_path, monkeypatch)
    client = TestClient(api_app)

    # Skip ingest entirely. rag_chunks is empty for this tmp_path.

    inbound = client.post(
        "/conversations/inbound",
        json={
            "text": _BUGGY_TOUR_QUERY,
            "chat_id": 999002,
            "customer_username": "test_customer",
            "trace_id": "e2e-pdf-buggy-empty",
        },
    )
    assert inbound.status_code == 200, inbound.text
    body = inbound.json()
    from services.api.app.answerers.scope_guard import RESPONSE_MODE_SCOPE_DECLINE
    assert body["delivered"] is True, body
    assert body["escalated"] is False, body
    assert body["response_mode"] == RESPONSE_MODE_SCOPE_DECLINE, body
