"""API-level tests for the Story 09.07 delete routes.

DELETE /admin/files/{short_id} and DELETE /admin/files?confirm=true exercise
the OperatorFilesAdminWriter cascade across the three SQLite databases. These
tests pin the auth scope rules and the cross-DB cascade semantics.
"""

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


def _attach(name: str = "x.pdf", size: int = 100) -> TelegramAttachment:
    return TelegramAttachment(
        file_id="tg-" + name,
        kind="document",
        mime_type="application/pdf",
        file_size=size,
        file_name=name,
    )


@pytest.fixture
def delete_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
    operator_files_db = tmp_path / "op_files.db"
    files_repo = OperatorFileRepository(db_path=str(operator_files_db))
    knowledge_db = tmp_path / "knowledge.db"
    moderation_repo = KnowledgeModerationRepository(db_path=str(knowledge_db))
    rag_db = tmp_path / "rag.db"
    rag_repo = RagRepository(db_path=str(rag_db))
    web_auth_db = tmp_path / "web_auth.db"
    web_auth_repo = WebAuthRepository(db_path=str(web_auth_db))

    from services.api.app.operators import OperatorRepository as _OperatorRepository
    operators_db = tmp_path / "operators.sqlite3"
    _op_repo = _OperatorRepository(str(operators_db))
    _op_repo.create(username="@alice", project_id=1, chat_id=4242)

    monkeypatch.setattr(api_main.settings, "operator_files_db_path", str(operator_files_db))
    monkeypatch.setattr(api_main.settings, "knowledge_db_path", str(knowledge_db))
    monkeypatch.setattr(api_main.settings, "rag_db_path", str(rag_db))
    monkeypatch.setattr(api_main.settings, "web_auth_db_path", str(web_auth_db))
    monkeypatch.setattr(api_main.settings, "operators_db_path", str(operators_db))
    monkeypatch.setattr(api_main.settings, "hitl_config_admin_username", "@ajdevy")
    monkeypatch.setattr(api_main.settings, "web_session_cookie_secure", False)
    monkeypatch.setattr(api_main.settings, "internal_service_token", "test-bot-token")
    monkeypatch.setattr(api_main, "web_auth_repository", web_auth_repo)
    monkeypatch.setattr(api_main, "knowledge_moderation_repository", moderation_repo)
    monkeypatch.setattr(api_main, "rag_repository", rag_repo)
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
    # The view + admin writer captured DB paths at import time — rebind to
    # the test temp DBs so the closure-bound routers hit the right files.
    monkeypatch.setattr(
        api_main.operator_files_view, "operator_files_db_path", str(operator_files_db)
    )
    monkeypatch.setattr(
        api_main.operator_files_view, "knowledge_db_path", str(knowledge_db)
    )
    monkeypatch.setattr(
        api_main.operator_files_admin_writer,
        "operator_files_db_path",
        str(operator_files_db),
    )
    monkeypatch.setattr(
        api_main.operator_files_admin_writer,
        "knowledge_db_path",
        str(knowledge_db),
    )
    monkeypatch.setattr(
        api_main.operator_files_admin_writer, "rag_db_path", str(rag_db)
    )

    client = TestClient(api_app)
    yield {
        "client": client,
        "files_repo": files_repo,
        "moderation_repo": moderation_repo,
        "rag_repo": rag_repo,
        "web_auth_repo": web_auth_repo,
        "tmp_path": tmp_path,
    }


def _seed(
    *,
    files_repo: OperatorFileRepository,
    moderation_repo: KnowledgeModerationRepository,
    rag_repo: RagRepository,
    username: str,
    chat_id: int,
    name: str,
    candidate_text: str,
    is_confidential: bool = False,
    stored_binary: Path | None = None,
    link_candidate: bool = True,
) -> tuple[str, int | None]:
    moderation_row = moderation_repo.create_pending(text=candidate_text)
    binary_path: str | None = None
    if stored_binary is not None:
        stored_binary.write_bytes(b"binary-bytes")
        binary_path = str(stored_binary)
    record = files_repo.record_upload(
        chat_id=chat_id,
        username=username,
        source_message_id=1,
        attachment=_attach(name=name, size=len(candidate_text)),
        is_confidential=is_confidential,
        stored_binary_path=binary_path,
        download_status="ok",
        source_file_type="pdf",
        kb_ingest_status="ok",
        kb_inserted_chunks=2,
    )
    candidate_id: int | None = None
    if link_candidate:
        files_repo.set_candidate_id(
            short_id=record.short_id, knowledge_candidate_id=moderation_row.id
        )
        rag_repo.ingest(
            source_id=f"knowledge_candidate:{moderation_row.id}",
            text=candidate_text,
        )
        candidate_id = moderation_row.id
    return record.short_id, candidate_id


def _login_as(client: TestClient, web_auth_repo: WebAuthRepository, username: str) -> None:
    chat_id = 4242 if username != "@ajdevy" else 1111
    code = web_auth_repo.create_code(username=username, chat_id=chat_id)
    response = client.post(
        "/admin/auth/verify", json={"username": username, "code": code}
    )
    assert response.status_code == 200, response.text


def test_delete_single_operator_own_file(delete_env: dict[str, Any]) -> None:
    short_id, _ = _seed(
        files_repo=delete_env["files_repo"],
        moderation_repo=delete_env["moderation_repo"],
        rag_repo=delete_env["rag_repo"],
        username="@alice",
        chat_id=4242,
        name="alice.pdf",
        candidate_text="alice file content\nsecond line",
    )
    client: TestClient = delete_env["client"]
    _login_as(client, delete_env["web_auth_repo"], "@alice")

    resp = client.delete(f"/admin/files/{short_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted_files"] == 1
    assert body["deleted_chunks"] == 2
    assert body["deleted_candidates"] == 1
    assert body["deleted_binaries"] == 0  # no binary stored
    assert body["failed_binary_paths"] == []

    # Second DELETE on same short_id → 404
    resp2 = client.delete(f"/admin/files/{short_id}")
    assert resp2.status_code == 404


def test_delete_single_unlinks_stored_binary(delete_env: dict[str, Any]) -> None:
    binary_path = delete_env["tmp_path"] / "alice.bin"
    short_id, _ = _seed(
        files_repo=delete_env["files_repo"],
        moderation_repo=delete_env["moderation_repo"],
        rag_repo=delete_env["rag_repo"],
        username="@alice",
        chat_id=4242,
        name="alice.pdf",
        candidate_text="hello",
        stored_binary=binary_path,
    )
    assert binary_path.exists()
    client: TestClient = delete_env["client"]
    _login_as(client, delete_env["web_auth_repo"], "@alice")

    resp = client.delete(f"/admin/files/{short_id}")
    assert resp.status_code == 200
    assert resp.json()["deleted_binaries"] == 1
    assert not binary_path.exists()


def test_delete_single_operator_other_owner_returns_404(
    delete_env: dict[str, Any],
) -> None:
    bob_short_id, _ = _seed(
        files_repo=delete_env["files_repo"],
        moderation_repo=delete_env["moderation_repo"],
        rag_repo=delete_env["rag_repo"],
        username="@bob",
        chat_id=5555,
        name="bob.pdf",
        candidate_text="bob secret",
    )
    # Alice needs a chat_id row for login to resolve her DM target.
    delete_env["files_repo"].record_upload(
        chat_id=4242,
        username="@alice",
        source_message_id=99,
        attachment=_attach(name="alice_seed.pdf"),
        is_confidential=False,
        stored_binary_path=None,
        download_status="ok",
        source_file_type="pdf",
    )
    client: TestClient = delete_env["client"]
    _login_as(client, delete_env["web_auth_repo"], "@alice")
    resp = client.delete(f"/admin/files/{bob_short_id}")
    assert resp.status_code == 404
    # Bob's row still exists.
    assert delete_env["files_repo"].get(short_id=bob_short_id) is not None


def test_delete_single_admin_can_delete_others_file(
    delete_env: dict[str, Any],
) -> None:
    short_id, candidate_id = _seed(
        files_repo=delete_env["files_repo"],
        moderation_repo=delete_env["moderation_repo"],
        rag_repo=delete_env["rag_repo"],
        username="@alice",
        chat_id=4242,
        name="alice.pdf",
        candidate_text="confidential secret",
        is_confidential=True,
    )
    client: TestClient = delete_env["client"]
    _login_as(client, delete_env["web_auth_repo"], "@ajdevy")
    resp = client.delete(f"/admin/files/{short_id}")
    assert resp.status_code == 200
    assert resp.json()["deleted_files"] == 1
    # Cascade reached the moderation row.
    assert candidate_id is not None
    with pytest.raises(LookupError):
        delete_env["moderation_repo"].get(candidate_id)
    # Cascade reached rag_chunks.
    hits = delete_env["rag_repo"].retrieve(query="confidential", limit=5)
    assert hits == []


def test_delete_single_unknown_short_id_returns_404(
    delete_env: dict[str, Any],
) -> None:
    client: TestClient = delete_env["client"]
    _login_as(client, delete_env["web_auth_repo"], "@ajdevy")
    resp = client.delete("/admin/files/NOPE1234")
    assert resp.status_code == 404


def test_delete_single_via_internal_token_and_as_user(
    delete_env: dict[str, Any],
) -> None:
    short_id, _ = _seed(
        files_repo=delete_env["files_repo"],
        moderation_repo=delete_env["moderation_repo"],
        rag_repo=delete_env["rag_repo"],
        username="@alice",
        chat_id=4242,
        name="alice.pdf",
        candidate_text="content via bot",
    )
    client: TestClient = delete_env["client"]
    resp = client.delete(
        f"/admin/files/{short_id}",
        params={"as_user": "@alice"},
        headers={"Authorization": "Bearer test-bot-token"},
    )
    assert resp.status_code == 200


def test_delete_single_internal_token_missing_as_user_returns_400(
    delete_env: dict[str, Any],
) -> None:
    client: TestClient = delete_env["client"]
    resp = client.delete(
        "/admin/files/ANY",
        headers={"Authorization": "Bearer test-bot-token"},
    )
    assert resp.status_code == 400


def test_delete_single_requires_session_without_internal_token(
    delete_env: dict[str, Any],
) -> None:
    client: TestClient = delete_env["client"]
    resp = client.delete("/admin/files/ANY")
    assert resp.status_code == 401


def test_delete_single_skips_rag_when_candidate_link_missing(
    delete_env: dict[str, Any],
) -> None:
    short_id, _ = _seed(
        files_repo=delete_env["files_repo"],
        moderation_repo=delete_env["moderation_repo"],
        rag_repo=delete_env["rag_repo"],
        username="@alice",
        chat_id=4242,
        name="orphan.pdf",
        candidate_text="orphaned content",
        link_candidate=False,
    )
    client: TestClient = delete_env["client"]
    _login_as(client, delete_env["web_auth_repo"], "@alice")
    resp = client.delete(f"/admin/files/{short_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted_files"] == 1
    assert body["deleted_candidates"] == 0
    assert body["deleted_chunks"] == 0


def test_delete_all_requires_confirm(delete_env: dict[str, Any]) -> None:
    client: TestClient = delete_env["client"]
    _login_as(client, delete_env["web_auth_repo"], "@ajdevy")
    resp = client.delete("/admin/files")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "confirm_required"


def test_delete_all_operator_scopes_to_self(delete_env: dict[str, Any]) -> None:
    for i in range(3):
        _seed(
            files_repo=delete_env["files_repo"],
            moderation_repo=delete_env["moderation_repo"],
            rag_repo=delete_env["rag_repo"],
            username="@alice",
            chat_id=4242,
            name=f"alice_{i}.pdf",
            candidate_text=f"alice content {i}",
        )
    bob_short_id, _ = _seed(
        files_repo=delete_env["files_repo"],
        moderation_repo=delete_env["moderation_repo"],
        rag_repo=delete_env["rag_repo"],
        username="@bob",
        chat_id=5555,
        name="bob.pdf",
        candidate_text="bob content",
    )
    client: TestClient = delete_env["client"]
    _login_as(client, delete_env["web_auth_repo"], "@alice")
    resp = client.delete("/admin/files", params={"confirm": "true"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted_files"] == 3
    assert body["deleted_candidates"] == 3
    # Bob's data untouched.
    assert delete_env["files_repo"].get(short_id=bob_short_id) is not None
    bob_chunks = delete_env["rag_repo"].retrieve(query="bob content", limit=5)
    assert any("bob content" in chunk.chunk_text for chunk in bob_chunks)


def test_delete_all_admin_scopes_to_own_username(
    delete_env: dict[str, Any],
) -> None:
    # Admin uploads their own file.
    _seed(
        files_repo=delete_env["files_repo"],
        moderation_repo=delete_env["moderation_repo"],
        rag_repo=delete_env["rag_repo"],
        username="@ajdevy",
        chat_id=1111,
        name="admin_own.pdf",
        candidate_text="admin own content",
    )
    alice_short_id, _ = _seed(
        files_repo=delete_env["files_repo"],
        moderation_repo=delete_env["moderation_repo"],
        rag_repo=delete_env["rag_repo"],
        username="@alice",
        chat_id=4242,
        name="alice.pdf",
        candidate_text="alice content",
    )
    client: TestClient = delete_env["client"]
    _login_as(client, delete_env["web_auth_repo"], "@ajdevy")
    resp = client.delete("/admin/files", params={"confirm": "true"})
    assert resp.status_code == 200
    assert resp.json()["deleted_files"] == 1
    # Alice's file survives because admin's bulk delete is own-only.
    assert delete_env["files_repo"].get(short_id=alice_short_id) is not None


def test_delete_all_returns_zero_when_no_files(delete_env: dict[str, Any]) -> None:
    client: TestClient = delete_env["client"]
    _login_as(client, delete_env["web_auth_repo"], "@alice")
    resp = client.delete("/admin/files", params={"confirm": "true"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "deleted_files": 0,
        "deleted_chunks": 0,
        "deleted_candidates": 0,
        "deleted_binaries": 0,
        "failed_binary_paths": [],
    }


def test_delete_all_internal_token_path(delete_env: dict[str, Any]) -> None:
    _seed(
        files_repo=delete_env["files_repo"],
        moderation_repo=delete_env["moderation_repo"],
        rag_repo=delete_env["rag_repo"],
        username="@alice",
        chat_id=4242,
        name="bot_path.pdf",
        candidate_text="bot path content",
    )
    client: TestClient = delete_env["client"]
    resp = client.delete(
        "/admin/files",
        params={"as_user": "@alice", "confirm": "true"},
        headers={"Authorization": "Bearer test-bot-token"},
    )
    assert resp.status_code == 200
    assert resp.json()["deleted_files"] == 1
