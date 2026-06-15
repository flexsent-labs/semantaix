from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.knowledge_moderation import KnowledgeModerationRepository
from services.api.app.main import app as api_app
from services.api.app.rag import RagRepository
from services.api.app.web_auth import WebAuthRepository
from services.bot_gateway.app.operator_files import OperatorFileRepository
from services.bot_gateway.app.telegram_update import TelegramAttachment


@pytest.fixture
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
    rag_db = tmp_path / "rag.db"
    operator_files_db = tmp_path / "operator_files.db"
    knowledge_db = tmp_path / "knowledge.db"
    web_auth_db = tmp_path / "web_auth.db"

    rag_repo = RagRepository(db_path=str(rag_db))
    files_repo = OperatorFileRepository(db_path=str(operator_files_db))
    moderation_repo = KnowledgeModerationRepository(db_path=str(knowledge_db))
    web_auth_repo = WebAuthRepository(db_path=str(web_auth_db))

    api_main.rag_repository.db_path = str(rag_db)
    monkeypatch.setattr(api_main.settings, "operator_files_db_path", str(operator_files_db))
    monkeypatch.setattr(api_main.settings, "web_auth_db_path", str(web_auth_db))
    monkeypatch.setattr(api_main.settings, "hitl_config_admin_username", "@ajdevy")
    monkeypatch.setattr(api_main.settings, "web_session_cookie_secure", False)
    monkeypatch.setattr(api_main.settings, "internal_service_token", "test-token")
    monkeypatch.setattr(api_main, "web_auth_repository", web_auth_repo)
    monkeypatch.setattr(api_main, "knowledge_moderation_repository", moderation_repo)
    monkeypatch.setattr(
        api_main.admin_auth_service, "web_auth_repository", web_auth_repo
    )
    monkeypatch.setattr(api_main.admin_auth_service, "settings", api_main.settings)
    monkeypatch.setattr(
        api_main.telegram_bot_sender,
        "send_message",
        AsyncMock(return_value={"ok": True}),
    )
    monkeypatch.setattr(
        api_main.admin_auth_service,
        "telegram_bot_sender",
        api_main.telegram_bot_sender,
    )
    client = TestClient(api_app)
    yield {
        "client": client,
        "rag_repo": rag_repo,
        "files_repo": files_repo,
        "moderation_repo": moderation_repo,
        "web_auth_repo": web_auth_repo,
    }


def _login(client: TestClient, web_auth_repo: WebAuthRepository, username: str) -> None:
    code = web_auth_repo.create_code(
        username=username, chat_id=1111 if username == "@ajdevy" else 4242
    )
    resp = client.post(
        "/admin/auth/verify", json={"username": username, "code": code}
    )
    assert resp.status_code == 200, resp.text


def test_inspect_requires_auth(env: dict[str, Any]) -> None:
    client: TestClient = env["client"]
    response = client.get("/admin/rag/inspect", params={"query": "багги тур"})
    assert response.status_code == 401


def test_inspect_via_internal_token(env: dict[str, Any]) -> None:
    env["rag_repo"].ingest(
        source_id="kb-buggy",
        text="Багги-тур по дюнам. Стоимость 2500 руб.",
    )
    client: TestClient = env["client"]
    response = client.get(
        "/admin/rag/inspect",
        params={"query": "хочу поехать на багги тур", "as_user": "@ajdevy"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["query"] == "хочу поехать на багги тур"
    assert "хотеть" in body["lemmas_stopwords_removed"]
    assert "тур" in body["lemmas_content"]
    assert body["denominator"] == len(body["lemmas_content"])
    assert body["candidates"], "expected at least one candidate"
    top = body["candidates"][0]
    assert top["source_id"] == "kb-buggy"
    assert top["score"] >= 0.6
    assert "тур" in top["matched_lemmas"]
    assert body["top_chunk_passes_threshold"] is True
    assert body["threshold"] == 0.6


def test_inspect_via_cookie_session(env: dict[str, Any]) -> None:
    env["rag_repo"].ingest(source_id="kb-1", text="alpha beta gamma")
    client: TestClient = env["client"]
    _login(client, env["web_auth_repo"], "@ajdevy")
    response = client.get(
        "/admin/rag/inspect", params={"query": "alpha"}
    )
    assert response.status_code == 200
    assert response.json()["query"] == "alpha"


def test_inspect_joins_operator_files(env: dict[str, Any]) -> None:
    candidate = env["moderation_repo"].create_pending(text="Багги-тур по дюнам.")
    env["rag_repo"].ingest(
        source_id=f"knowledge_candidate:{candidate.id}",
        text="Багги-тур по дюнам. Стоимость 2500 руб.",
    )
    record = env["files_repo"].record_upload(
        chat_id=4242,
        username="@alice",
        source_message_id=1,
        attachment=TelegramAttachment(
            file_id="tg-buggy.pdf",
            kind="document",
            mime_type="application/pdf",
            file_size=100,
            file_name="buggy_tour.pdf",
        ),
        is_confidential=False,
        stored_binary_path=None,
        download_status="ok",
        source_file_type="pdf",
        kb_ingest_status="ingested",
        kb_inserted_chunks=2,
    )
    env["files_repo"].set_candidate_id(
        short_id=record.short_id, knowledge_candidate_id=candidate.id
    )
    client: TestClient = env["client"]
    response = client.get(
        "/admin/rag/inspect",
        params={"query": "багги тур", "as_user": "@ajdevy"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["operator_files"], "expected joined operator_files entry"
    joined = body["operator_files"][0]
    assert joined["source_file_name"] == "buggy_tour.pdf"
    assert joined["kb_ingest_status"] == "ingested"
    assert joined["uploaded_by"] == "@alice"
    summary = body["kb_ingest_status_summary"]
    assert summary.get("ingested", 0) >= 1


def test_inspect_passes_project_id_override(env: dict[str, Any]) -> None:
    env["rag_repo"].ingest(
        source_id="kb-a", text="Багги-тур проект A", project_id=1
    )
    env["rag_repo"].ingest(
        source_id="kb-b", text="Багги-тур проект B", project_id=2
    )
    client: TestClient = env["client"]
    response = client.get(
        "/admin/rag/inspect",
        params={
            "query": "багги тур", "project_id": 1, "as_user": "@ajdevy"
        },
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    source_ids = {c["source_id"] for c in response.json()["candidates"]}
    assert source_ids == {"kb-a"}


def test_inspect_handles_missing_operator_files_db(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        api_main.settings,
        "operator_files_db_path",
        str(tmp_path / "does_not_exist.db"),
    )
    env["rag_repo"].ingest(source_id="kb-1", text="alpha beta gamma")
    client: TestClient = env["client"]
    response = client.get(
        "/admin/rag/inspect",
        params={"query": "alpha", "as_user": "@ajdevy"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["operator_files"] == []
    assert body["kb_ingest_status_summary"] == {}


def test_inspect_handles_non_candidate_source_id(env: dict[str, Any]) -> None:
    # source_id without "knowledge_candidate:" prefix maps to no operator file.
    env["rag_repo"].ingest(source_id="custom-kb", text="alpha beta gamma")
    client: TestClient = env["client"]
    response = client.get(
        "/admin/rag/inspect",
        params={"query": "alpha", "as_user": "@ajdevy"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    assert response.json()["operator_files"] == []


def test_inspect_handles_malformed_candidate_source_id(env: dict[str, Any]) -> None:
    env["rag_repo"].ingest(
        source_id="knowledge_candidate:not_an_int", text="alpha"
    )
    client: TestClient = env["client"]
    response = client.get(
        "/admin/rag/inspect",
        params={"query": "alpha", "as_user": "@ajdevy"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    assert response.json()["operator_files"] == []


def test_inspect_returns_empty_files_when_no_candidates(
    env: dict[str, Any]
) -> None:
    client: TestClient = env["client"]
    response = client.get(
        "/admin/rag/inspect",
        params={"query": "no_match_anywhere", "as_user": "@ajdevy"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["candidates"] == []
    assert body["operator_files"] == []


def test_inspect_chat_id_resolves_project(env: dict[str, Any]) -> None:
    env["rag_repo"].ingest(
        source_id="kb-null", text="Багги-тур без проекта"
    )
    client: TestClient = env["client"]
    response = client.get(
        "/admin/rag/inspect",
        params={"query": "багги тур", "chat_id": 999, "as_user": "@ajdevy"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["chat_id"] == 999
    # chat_id=999 has no ticket → resolution falls to default project.
    assert body["resolved_project_id"] == body["default_project_id"]
