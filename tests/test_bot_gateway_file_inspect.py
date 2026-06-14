from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.bot_gateway.app import main as bot_main
from services.bot_gateway.app.main import app as bot_app


@pytest.fixture
def isolated_inspect_bot(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
    monkeypatch.setattr(bot_main.settings, "persistence_db_path", str(tmp_path / "story.db"))
    monkeypatch.setattr(bot_main.settings, "hitl_ticket_db_path", str(tmp_path / "hitl.db"))
    monkeypatch.setattr(bot_main.settings, "telegram_bot_token", "TKN")
    monkeypatch.setattr(bot_main.settings, "hitl_config_admin_username", "@ajdevy")

    async def _op_lookup(*, username: str):
        if username == "@alice":
            return {"username": "@alice", "chat_id": 100, "project_id": 1, "is_active": True}
        return None

    monkeypatch.setattr(bot_main.api_client, "find_operator_by_username", _op_lookup)

    class _StubHitlRepo:
        def get_runtime_config(self, key: str) -> str | None:
            return None

    monkeypatch.setattr(bot_main, "hitl_ticket_repository", _StubHitlRepo())

    dms: list[tuple[int, str]] = []

    async def fake_send_dm(chat_id: int, text: str) -> None:
        dms.append((chat_id, text))

    monkeypatch.setattr(bot_main, "_send_dm", fake_send_dm)

    client = TestClient(bot_app)
    yield {"client": client, "dms": dms}


def _operator_message(text: str, username: str = "@alice") -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": 100},
            "from": {"id": 200, "username": username.lstrip("@")},
            "text": text,
        },
    }


def test_file_command_dms_metadata_and_extracted_text_head(
    isolated_inspect_bot: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(**kwargs: Any) -> dict:
        return {
            "short_id": "ABC2XYZ9",
            "source_file_name": "policy.pdf",
            "source_file_type": "pdf",
            "uploaded_by": "@alice",
            "uploaded_at": "2026-05-12T09:33:00+00:00",
            "file_size_bytes": 412 * 1024,
            "is_confidential": True,
            "kb_ingest_status": "ok",
            "kb_inserted_chunks": 14,
            "candidate_text": "lorem ipsum " * 500,
        }

    monkeypatch.setattr(
        bot_main.api_client, "fetch_file_inspect", AsyncMock(side_effect=fake_fetch)
    )

    client: TestClient = isolated_inspect_bot["client"]
    response = client.post(
        "/telegram/webhook", json=_operator_message("/file ABC2XYZ9")
    )
    assert response.status_code == 200
    dms = isolated_inspect_bot["dms"]
    assert len(dms) == 1
    _, text = dms[0]
    assert "ABC2XYZ9" in text
    assert "policy.pdf" in text
    assert "@alice" in text
    assert "конфиденциально" in text
    assert "Извлечённый текст" in text
    assert "lorem" in text


def test_file_command_unknown_short_id_shows_not_found(
    isolated_inspect_bot: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        bot_main.api_client, "fetch_file_inspect", AsyncMock(return_value=None)
    )
    client: TestClient = isolated_inspect_bot["client"]
    response = client.post(
        "/telegram/webhook", json=_operator_message("/file UNKNOWN1")
    )
    assert response.status_code == 200
    dms = isolated_inspect_bot["dms"]
    assert len(dms) == 1
    _, text = dms[0]
    assert "не найден" in text.lower()
    assert "UNKNOWN1" in text


def test_file_command_missing_arg_shows_usage(
    isolated_inspect_bot: dict[str, Any],
) -> None:
    client: TestClient = isolated_inspect_bot["client"]
    response = client.post("/telegram/webhook", json=_operator_message("/file"))
    assert response.status_code == 200
    _, text = isolated_inspect_bot["dms"][0]
    assert "Использование" in text or "/file" in text


def test_file_command_no_extracted_text_uses_status_explanation(
    isolated_inspect_bot: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(**kwargs: Any) -> dict:
        return {
            "short_id": "ABC2XYZ9",
            "source_file_name": "big.pdf",
            "source_file_type": "pdf",
            "uploaded_by": "@alice",
            "uploaded_at": "2026-05-12T09:33:00+00:00",
            "file_size_bytes": 99999999,
            "is_confidential": False,
            "kb_ingest_status": "skipped: file_too_large",
            "kb_inserted_chunks": None,
            "candidate_text": None,
        }

    monkeypatch.setattr(
        bot_main.api_client, "fetch_file_inspect", AsyncMock(side_effect=fake_fetch)
    )
    client: TestClient = isolated_inspect_bot["client"]
    response = client.post(
        "/telegram/webhook", json=_operator_message("/file ABC2XYZ9")
    )
    assert response.status_code == 200
    _, text = isolated_inspect_bot["dms"][0]
    assert "Извлечение текста недоступно" in text
    assert "file_too_large" in text


def test_files_find_command_returns_snippets(
    isolated_inspect_bot: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_search(**kwargs: Any) -> dict:
        return {
            "total": 2,
            "items": [
                {
                    "short_id": "ABC2XYZ9",
                    "source_file_name": "policy.pdf",
                    "uploaded_by": "@alice",
                    "uploaded_at": "2026-05-12T09:33:00+00:00",
                    "snippet": "…договор между сторонами…",
                },
                {
                    "short_id": "X8N4M2QT",
                    "source_file_name": "sla.docx",
                    "uploaded_by": "@alice",
                    "uploaded_at": "2026-05-10T10:00:00+00:00",
                    "snippet": "…договор оферты…",
                },
            ],
        }

    monkeypatch.setattr(
        bot_main.api_client, "search_files", AsyncMock(side_effect=fake_search)
    )
    client: TestClient = isolated_inspect_bot["client"]
    response = client.post(
        "/telegram/webhook", json=_operator_message("/files_find договор")
    )
    assert response.status_code == 200
    _, text = isolated_inspect_bot["dms"][0]
    assert "Найдено" in text or "найдено" in text
    assert "ABC2XYZ9" in text
    assert "X8N4M2QT" in text
    assert "policy.pdf" in text
    assert "договор" in text


def test_files_find_empty_query_shows_usage(
    isolated_inspect_bot: dict[str, Any],
) -> None:
    client: TestClient = isolated_inspect_bot["client"]
    response = client.post(
        "/telegram/webhook", json=_operator_message("/files_find")
    )
    assert response.status_code == 200
    _, text = isolated_inspect_bot["dms"][0]
    assert "Использование" in text


def test_files_find_no_hits_shows_nothing_found(
    isolated_inspect_bot: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        bot_main.api_client,
        "search_files",
        AsyncMock(return_value={"total": 0, "items": []}),
    )
    client: TestClient = isolated_inspect_bot["client"]
    response = client.post(
        "/telegram/webhook", json=_operator_message("/files_find никогда")
    )
    assert response.status_code == 200
    _, text = isolated_inspect_bot["dms"][0]
    assert "Ничего не найдено" in text


def test_file_command_from_non_operator_non_admin_is_ignored(
    isolated_inspect_bot: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch = AsyncMock()
    monkeypatch.setattr(bot_main.api_client, "fetch_file_inspect", fetch)
    client: TestClient = isolated_inspect_bot["client"]
    response = client.post(
        "/telegram/webhook",
        json=_operator_message("/file ABC2XYZ9", username="@stranger"),
    )
    # Either ignored or forwarded — but no fetch_file_inspect call.
    assert fetch.await_count == 0
    assert response.status_code == 200


def test_file_command_works_for_admin(
    isolated_inspect_bot: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(**kwargs: Any) -> dict:
        return {
            "short_id": "ABC2XYZ9",
            "source_file_name": "bobfile.pdf",
            "source_file_type": "pdf",
            "uploaded_by": "@bob",
            "uploaded_at": "2026-05-12T09:33:00+00:00",
            "file_size_bytes": 1024,
            "is_confidential": False,
            "kb_ingest_status": "ok",
            "kb_inserted_chunks": 3,
            "candidate_text": "admin can see this",
        }

    monkeypatch.setattr(
        bot_main.api_client, "fetch_file_inspect", AsyncMock(side_effect=fake_fetch)
    )
    client: TestClient = isolated_inspect_bot["client"]
    response = client.post(
        "/telegram/webhook",
        json=_operator_message("/file ABC2XYZ9", username="@ajdevy"),
    )
    assert response.status_code == 200
    _, text = isolated_inspect_bot["dms"][0]
    assert "bobfile.pdf" in text
    assert "admin can see this" in text
