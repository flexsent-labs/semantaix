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
def files_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
    operator_files_db = tmp_path / "op_files.db"
    files_repo = OperatorFileRepository(db_path=str(operator_files_db))
    knowledge_db = tmp_path / "knowledge.db"
    moderation_repo = KnowledgeModerationRepository(db_path=str(knowledge_db))
    web_auth_db = tmp_path / "web_auth.db"
    web_auth_repo = WebAuthRepository(db_path=str(web_auth_db))

    monkeypatch.setattr(api_main.settings, "operator_files_db_path", str(operator_files_db))
    monkeypatch.setattr(api_main.settings, "knowledge_db_path", str(knowledge_db))
    monkeypatch.setattr(api_main.settings, "web_auth_db_path", str(web_auth_db))
    monkeypatch.setattr(api_main.settings, "hitl_config_admin_username", "@ajdevy")
    monkeypatch.setattr(api_main.settings, "web_session_cookie_secure", False)
    monkeypatch.setattr(api_main.settings, "internal_service_token", "test-bot-token")
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
    # The router captured operator_files_view by reference at wire time —
    # mutate the original object in place so the closure sees the test DBs.
    monkeypatch.setattr(
        api_main.operator_files_view, "operator_files_db_path", str(operator_files_db)
    )
    monkeypatch.setattr(
        api_main.operator_files_view, "knowledge_db_path", str(knowledge_db)
    )

    client = TestClient(api_app)
    yield {
        "client": client,
        "files_repo": files_repo,
        "moderation_repo": moderation_repo,
        "web_auth_repo": web_auth_repo,
    }


def _seed_file(
    files_repo: OperatorFileRepository,
    moderation_repo: KnowledgeModerationRepository,
    *,
    username: str,
    chat_id: int,
    name: str,
    candidate_text: str,
    is_confidential: bool = False,
    set_candidate_link: bool = True,
) -> str:
    moderation_row = moderation_repo.create_pending(text=candidate_text)
    record = files_repo.record_upload(
        chat_id=chat_id,
        username=username,
        source_message_id=1,
        attachment=_attach(name=name, size=len(candidate_text)),
        is_confidential=is_confidential,
        stored_binary_path=None,
        download_status="ok",
        source_file_type="pdf",
        kb_ingest_status="ok",
        kb_inserted_chunks=3,
    )
    if set_candidate_link:
        files_repo.set_candidate_id(
            short_id=record.short_id, knowledge_candidate_id=moderation_row.id
        )
    return record.short_id


def _login_as(client: TestClient, web_auth_repo: WebAuthRepository, username: str) -> None:
    chat_id = 4242 if username != "@ajdevy" else 1111
    code = web_auth_repo.create_code(username=username, chat_id=chat_id)
    response = client.post(
        "/admin/auth/verify", json={"username": username, "code": code}
    )
    assert response.status_code == 200, response.text


def test_list_requires_session(files_env: dict[str, Any]) -> None:
    client: TestClient = files_env["client"]
    response = client.get("/admin/files")
    assert response.status_code == 401


def test_list_returns_only_own_files_for_operator(files_env: dict[str, Any]) -> None:
    _seed_file(
        files_env["files_repo"],
        files_env["moderation_repo"],
        username="@alice",
        chat_id=4242,
        name="alice1.pdf",
        candidate_text="alice file content",
    )
    _seed_file(
        files_env["files_repo"],
        files_env["moderation_repo"],
        username="@bob",
        chat_id=5555,
        name="bob1.pdf",
        candidate_text="bob file content",
    )
    # Alice has no chat_id row yet — seed one separately to enable her login.
    files_env["files_repo"].record_upload(
        chat_id=4242,
        username="@alice",
        source_message_id=99,
        attachment=_attach(name="setup.pdf"),
        is_confidential=False,
        stored_binary_path=None,
        download_status="ok",
        source_file_type="pdf",
    )
    client: TestClient = files_env["client"]
    _login_as(client, files_env["web_auth_repo"], "@alice")
    response = client.get("/admin/files")
    assert response.status_code == 200
    body = response.json()
    names = [item["source_file_name"] for item in body["items"]]
    assert "alice1.pdf" in names
    assert "bob1.pdf" not in names


def test_list_returns_all_files_for_admin(files_env: dict[str, Any]) -> None:
    _seed_file(
        files_env["files_repo"],
        files_env["moderation_repo"],
        username="@alice",
        chat_id=4242,
        name="alice1.pdf",
        candidate_text="alice content",
    )
    _seed_file(
        files_env["files_repo"],
        files_env["moderation_repo"],
        username="@bob",
        chat_id=5555,
        name="bob1.pdf",
        candidate_text="bob content",
    )
    client: TestClient = files_env["client"]
    _login_as(client, files_env["web_auth_repo"], "@ajdevy")
    response = client.get("/admin/files")
    assert response.status_code == 200
    names = {item["source_file_name"] for item in response.json()["items"]}
    assert {"alice1.pdf", "bob1.pdf"}.issubset(names)


def test_detail_includes_extracted_text(files_env: dict[str, Any]) -> None:
    short_id = _seed_file(
        files_env["files_repo"],
        files_env["moderation_repo"],
        username="@alice",
        chat_id=4242,
        name="alice1.pdf",
        candidate_text="detailed extracted content",
    )
    client: TestClient = files_env["client"]
    _login_as(client, files_env["web_auth_repo"], "@alice")
    response = client.get(f"/admin/files/{short_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["short_id"] == short_id
    assert body["candidate_text"] == "detailed extracted content"


def test_detail_returns_404_for_unknown_short_id(files_env: dict[str, Any]) -> None:
    client: TestClient = files_env["client"]
    _login_as(client, files_env["web_auth_repo"], "@ajdevy")
    response = client.get("/admin/files/UNKNOWN1")
    assert response.status_code == 404


def test_operator_cannot_view_other_owners_file(files_env: dict[str, Any]) -> None:
    short_id = _seed_file(
        files_env["files_repo"],
        files_env["moderation_repo"],
        username="@bob",
        chat_id=5555,
        name="bob1.pdf",
        candidate_text="bob secret",
    )
    files_env["files_repo"].record_upload(
        chat_id=4242,
        username="@alice",
        source_message_id=1,
        attachment=_attach(name="setup.pdf"),
        is_confidential=False,
        stored_binary_path=None,
        download_status="ok",
        source_file_type="pdf",
    )
    client: TestClient = files_env["client"]
    _login_as(client, files_env["web_auth_repo"], "@alice")
    response = client.get(f"/admin/files/{short_id}")
    assert response.status_code == 404


def test_admin_can_view_confidential_file(files_env: dict[str, Any]) -> None:
    short_id = _seed_file(
        files_env["files_repo"],
        files_env["moderation_repo"],
        username="@alice",
        chat_id=4242,
        name="secret.pdf",
        candidate_text="top secret",
        is_confidential=True,
    )
    client: TestClient = files_env["client"]
    _login_as(client, files_env["web_auth_repo"], "@ajdevy")
    response = client.get(f"/admin/files/{short_id}")
    assert response.status_code == 200
    assert response.json()["is_confidential"] is True


def test_detail_renders_when_candidate_link_missing(files_env: dict[str, Any]) -> None:
    short_id = _seed_file(
        files_env["files_repo"],
        files_env["moderation_repo"],
        username="@alice",
        chat_id=4242,
        name="legacy.pdf",
        candidate_text="will not be linked",
        set_candidate_link=False,
    )
    client: TestClient = files_env["client"]
    _login_as(client, files_env["web_auth_repo"], "@alice")
    response = client.get(f"/admin/files/{short_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["candidate_text"] is None


def test_search_returns_hits_with_snippet(files_env: dict[str, Any]) -> None:
    _seed_file(
        files_env["files_repo"],
        files_env["moderation_repo"],
        username="@alice",
        chat_id=4242,
        name="contract.pdf",
        candidate_text="договор между сторонами вступает в силу с 1 января",
    )
    _seed_file(
        files_env["files_repo"],
        files_env["moderation_repo"],
        username="@alice",
        chat_id=4242,
        name="notes.pdf",
        candidate_text="что-то совсем другое без ключевого слова",
    )
    client: TestClient = files_env["client"]
    _login_as(client, files_env["web_auth_repo"], "@alice")
    response = client.get("/admin/files/search", params={"q": "договор"})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 1
    snippets = [hit["snippet"] for hit in body["items"]]
    assert any("договор" in s for s in snippets)


def test_search_returns_empty_for_short_query(files_env: dict[str, Any]) -> None:
    client: TestClient = files_env["client"]
    _login_as(client, files_env["web_auth_repo"], "@ajdevy")
    response = client.get("/admin/files/search", params={"q": "a"})
    assert response.status_code == 400


def test_search_scopes_to_uploader_for_operator(files_env: dict[str, Any]) -> None:
    _seed_file(
        files_env["files_repo"],
        files_env["moderation_repo"],
        username="@alice",
        chat_id=4242,
        name="alice_договор.pdf",
        candidate_text="alice mentions договор",
    )
    _seed_file(
        files_env["files_repo"],
        files_env["moderation_repo"],
        username="@bob",
        chat_id=5555,
        name="bob_договор.pdf",
        candidate_text="bob also mentions договор",
    )
    client: TestClient = files_env["client"]
    _login_as(client, files_env["web_auth_repo"], "@alice")
    response = client.get("/admin/files/search", params={"q": "договор"})
    assert response.status_code == 200
    names = {hit["source_file_name"] for hit in response.json()["items"]}
    assert "alice_договор.pdf" in names
    assert "bob_договор.pdf" not in names


def test_internal_token_lets_bot_query_as_user(files_env: dict[str, Any]) -> None:
    short_id = _seed_file(
        files_env["files_repo"],
        files_env["moderation_repo"],
        username="@alice",
        chat_id=4242,
        name="alice1.pdf",
        candidate_text="bot-fetched content",
    )
    client: TestClient = files_env["client"]
    response = client.get(
        f"/admin/files/{short_id}",
        params={"as_user": "@alice"},
        headers={"Authorization": "Bearer test-bot-token"},
    )
    assert response.status_code == 200
    assert response.json()["candidate_text"] == "bot-fetched content"


def test_internal_token_requires_as_user(files_env: dict[str, Any]) -> None:
    client: TestClient = files_env["client"]
    response = client.get(
        "/admin/files",
        headers={"Authorization": "Bearer test-bot-token"},
    )
    assert response.status_code == 400


def test_internal_token_wrong_value_falls_through_to_cookie(
    files_env: dict[str, Any],
) -> None:
    client: TestClient = files_env["client"]
    response = client.get(
        "/admin/files",
        params={"as_user": "@alice"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401


def test_list_admin_owner_filter(files_env: dict[str, Any]) -> None:
    _seed_file(
        files_env["files_repo"],
        files_env["moderation_repo"],
        username="@alice",
        chat_id=4242,
        name="alice1.pdf",
        candidate_text="alice",
    )
    _seed_file(
        files_env["files_repo"],
        files_env["moderation_repo"],
        username="@bob",
        chat_id=5555,
        name="bob1.pdf",
        candidate_text="bob",
    )
    client: TestClient = files_env["client"]
    _login_as(client, files_env["web_auth_repo"], "@ajdevy")
    response = client.get("/admin/files", params={"owner": "@bob"})
    assert response.status_code == 200
    names = {item["source_file_name"] for item in response.json()["items"]}
    assert names == {"bob1.pdf"}


def test_internal_token_unknown_as_user_returns_403(
    files_env: dict[str, Any],
) -> None:
    client: TestClient = files_env["client"]
    response = client.get(
        "/admin/files",
        params={"as_user": "@nobody"},
        headers={"Authorization": "Bearer test-bot-token"},
    )
    assert response.status_code == 403


def test_view_raises_on_missing_db_path(tmp_path: Path) -> None:
    from services.api.app.operator_files_view import OperatorFilesView

    view = OperatorFilesView(
        operator_files_db_path=str(tmp_path / "missing.db"),
        knowledge_db_path=str(tmp_path / "kdb.db"),
    )
    import pytest

    with pytest.raises(FileNotFoundError):
        view.list_files(
            viewer_username="@alice", viewer_role="operator", limit=10
        )


def test_search_snippet_falls_back_when_query_absent(files_env: dict[str, Any]) -> None:
    from services.api.app.operator_files_view import _build_snippet

    # Build a synthetic case where the text doesn't contain the query — this
    # exercises the no-match branch of the snippet builder.
    snippet = _build_snippet(text="lorem ipsum dolor sit amet", query="banana")
    assert "lorem" in snippet


def test_list_operator_owner_filter_ignored(files_env: dict[str, Any]) -> None:
    _seed_file(
        files_env["files_repo"],
        files_env["moderation_repo"],
        username="@alice",
        chat_id=4242,
        name="alice1.pdf",
        candidate_text="alice",
    )
    _seed_file(
        files_env["files_repo"],
        files_env["moderation_repo"],
        username="@bob",
        chat_id=5555,
        name="bob1.pdf",
        candidate_text="bob",
    )
    client: TestClient = files_env["client"]
    _login_as(client, files_env["web_auth_repo"], "@alice")
    # Operator tries to filter to @bob — must still only see their own.
    response = client.get("/admin/files", params={"owner": "@bob"})
    assert response.status_code == 200
    names = {item["source_file_name"] for item in response.json()["items"]}
    assert "bob1.pdf" not in names
