"""``/kb_add confidential`` — neither hook runs (Story 12.05c).

The KB-upload hook short-circuits on ``is_confidential=True`` BEFORE
either fan-out: confidential files must never contribute to client
materials (12.05b) nor the services catalog (12.05c). Defense in depth:
the api endpoints would also short-circuit if the bot ever forwarded
the call.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.bot_gateway.app import main as bot_main
from services.bot_gateway.app.kb_session import OperatorKbSessionRepository
from services.bot_gateway.app.main import app as bot_app
from services.bot_gateway.app.media_group_buffer import MediaGroupBuffer
from services.bot_gateway.app.operator_files import OperatorFileRepository
from services.bot_gateway.app.telegram_file_download import DownloadedFile


class _StubHitlRepo:
    def get_runtime_config(self, key: str) -> str | None:
        return None

    def set_runtime_config(self, **kwargs: Any) -> None:  # pragma: no cover
        pass

    def list_all(self) -> list:  # pragma: no cover
        return []


@pytest.fixture
def isolated_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(
        bot_main.settings, "persistence_db_path", str(tmp_path / "story.db")
    )
    monkeypatch.setattr(
        bot_main.settings, "hitl_ticket_db_path", str(tmp_path / "hitl.db")
    )
    monkeypatch.setattr(
        bot_main.settings,
        "operator_upload_storage_dir",
        str(tmp_path / "uploads"),
    )
    monkeypatch.setattr(bot_main.settings, "operator_upload_max_bytes", 1024)
    monkeypatch.setattr(bot_main.settings, "telegram_bot_token", "TKN")
    monkeypatch.setattr(
        bot_main.settings, "hitl_primary_operator_username", "@ajdevy"
    )
    monkeypatch.setattr(
        bot_main.settings, "internal_service_token", "bot-token-x"
    )
    monkeypatch.setattr(bot_main, "hitl_ticket_repository", _StubHitlRepo())
    monkeypatch.setattr(
        bot_main,
        "kb_session_repository",
        OperatorKbSessionRepository(str(tmp_path / "hitl.db")),
    )
    monkeypatch.setattr(
        bot_main,
        "operator_file_repository",
        OperatorFileRepository(str(tmp_path / "operator_files.db")),
    )
    monkeypatch.setattr(
        bot_main, "media_group_buffer", MediaGroupBuffer(str(tmp_path / "hitl.db"))
    )
    monkeypatch.setattr(
        bot_main.settings, "operator_media_group_debounce_seconds", 0
    )

    sent_dms: list[tuple[int, str]] = []

    async def fake_send_dm(chat_id: int, text: str) -> None:
        sent_dms.append((chat_id, text))

    monkeypatch.setattr(bot_main, "_send_dm", fake_send_dm)
    return {"tmp_path": tmp_path, "dms": sent_dms}


def _confidential_payload() -> dict[str, Any]:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": 100},
            "from": {"id": 200, "username": "ajdevy"},
            "caption": "/kb_add confidential",
            "document": {
                "file_id": "DOCXYZ",
                "file_name": "private.pdf",
                "mime_type": "application/pdf",
                "file_size": 100,
            },
        },
    }


def _patch_download(monkeypatch, tmp_path) -> None:
    file_path = tmp_path / "private.pdf"
    file_path.write_bytes(b"PDF")

    async def fake_download(self, *, file_id, suggested_extension, mime_type=None):
        return DownloadedFile(
            path=file_path, byte_size=3, mime_type=mime_type
        )

    monkeypatch.setattr(
        bot_main.TelegramFileDownloader, "download", fake_download, raising=False
    )


def test_confidential_upload_skips_both_hooks(
    isolated_bot, monkeypatch
) -> None:
    _patch_download(monkeypatch, isolated_bot["tmp_path"])

    async def fake_submit(**_kwargs):
        return {
            "inserted_chunks": 4,
            "is_confidential": True,
            "deduplicated": False,
            "candidate_id": 11,
            "project_id": 7,
        }

    analyze_calls: list[dict[str, Any]] = []
    extract_calls: list[dict[str, Any]] = []

    async def fake_analyze(**kwargs):
        analyze_calls.append(kwargs)
        return {"registered": True, "material_id": 1, "reason": "ok"}

    async def fake_extract(**kwargs):
        extract_calls.append(kwargs)
        return {
            "added": [{"service_id": 1, "name": "X"}],
            "skipped_existing": [],
            "reason": "ok",
        }

    monkeypatch.setattr(
        bot_main.api_client, "submit_operator_upload", fake_submit
    )
    monkeypatch.setattr(
        bot_main.api_client, "analyze_kb_material", fake_analyze, raising=False
    )
    monkeypatch.setattr(
        bot_main.api_client, "extract_kb_services", fake_extract, raising=False
    )

    response = TestClient(bot_app).post(
        "/telegram/webhook", json=_confidential_payload()
    )
    assert response.status_code == 200

    summary = [
        text for _, text in isolated_bot["dms"] if "Добавлено в базу" in text
    ][0]
    assert "📎 Добавлен в материалы" not in summary
    assert "📦 Услуги добавлены" not in summary

    # Neither fan-out hook should be called for a confidential upload.
    assert analyze_calls == []
    assert extract_calls == []
