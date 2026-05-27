"""Bot-gateway hook into the 12.05b client-materials analyzer.

After a successful KB ingest the bot calls
``POST /sales/materials/analyze-kb-file`` and, when the analyzer
returns ``registered=True``, appends one line per registered material
to the existing Russian KB-upload acknowledgement.

Failures of the analyze call are silent — KB ack is sent regardless,
the operator never sees the analyzer error, and the failure is logged
as ``sales_kb_material_analyze_failed``.

Confidential uploads are skipped entirely (no analyze call) — the
confidentiality flag also short-circuits server-side as defense in
depth.
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
    fresh_kb_repo = OperatorKbSessionRepository(str(tmp_path / "hitl.db"))
    monkeypatch.setattr(bot_main, "kb_session_repository", fresh_kb_repo)
    fresh_files_repo = OperatorFileRepository(
        str(tmp_path / "operator_files.db")
    )
    monkeypatch.setattr(bot_main, "operator_file_repository", fresh_files_repo)
    fresh_mg_buffer = MediaGroupBuffer(str(tmp_path / "hitl.db"))
    monkeypatch.setattr(bot_main, "media_group_buffer", fresh_mg_buffer)
    monkeypatch.setattr(
        bot_main.settings, "operator_media_group_debounce_seconds", 0
    )

    sent_dms: list[tuple[int, str]] = []

    async def fake_send_dm(chat_id: int, text: str) -> None:
        sent_dms.append((chat_id, text))

    monkeypatch.setattr(bot_main, "_send_dm", fake_send_dm)
    return {"tmp_path": tmp_path, "dms": sent_dms}


def _operator_kb_payload() -> dict[str, Any]:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": 100},
            "from": {"id": 200, "username": "ajdevy"},
            "caption": "/kb_add",
            "document": {
                "file_id": "DOCXYZ",
                "file_name": "tours.pdf",
                "mime_type": "application/pdf",
                "file_size": 100,
            },
        },
    }


def _operator_kb_confidential_payload() -> dict[str, Any]:
    payload = _operator_kb_payload()
    payload["message"]["caption"] = "/kb_add confidential"
    return payload


def _patch_download(monkeypatch, tmp_path) -> None:
    file_path = tmp_path / "tours.pdf"
    file_path.write_bytes(b"PDF")

    async def fake_download(self, *, file_id, suggested_extension, mime_type=None):
        return DownloadedFile(
            path=file_path, byte_size=3, mime_type=mime_type
        )

    monkeypatch.setattr(
        bot_main.TelegramFileDownloader, "download", fake_download, raising=False
    )


def test_registered_material_appends_clients_line(
    isolated_bot, monkeypatch
) -> None:
    _patch_download(monkeypatch, isolated_bot["tmp_path"])

    async def fake_submit(**kwargs):
        return {
            "inserted_chunks": 4,
            "is_confidential": False,
            "deduplicated": False,
            "candidate_id": 11,
            "project_id": 7,
        }

    monkeypatch.setattr(
        bot_main.api_client, "submit_operator_upload", fake_submit
    )

    analyze_calls: list[dict[str, Any]] = []

    async def fake_analyze(**kwargs):
        analyze_calls.append(kwargs)
        return {"registered": True, "material_id": 42, "reason": "ok"}

    monkeypatch.setattr(
        bot_main.api_client, "analyze_kb_material", fake_analyze, raising=False
    )

    response = TestClient(bot_app).post(
        "/telegram/webhook", json=_operator_kb_payload()
    )
    assert response.status_code == 200

    summary_dms = [
        text for _, text in isolated_bot["dms"] if "Добавлено в базу" in text
    ]
    assert summary_dms, isolated_bot["dms"]
    summary = summary_dms[0]
    assert "📎 Добавлен в материалы для клиентов (id=42)." in summary

    assert len(analyze_calls) == 1
    assert analyze_calls[0]["project_id"] == 7
    assert analyze_calls[0]["operator_file_short_id"]
    assert analyze_calls[0]["internal_token"] == "bot-token-x"


def test_not_registered_does_not_change_ack(
    isolated_bot, monkeypatch
) -> None:
    _patch_download(monkeypatch, isolated_bot["tmp_path"])

    async def fake_submit(**kwargs):
        return {
            "inserted_chunks": 4,
            "is_confidential": False,
            "deduplicated": False,
            "candidate_id": 11,
            "project_id": 7,
        }

    monkeypatch.setattr(
        bot_main.api_client, "submit_operator_upload", fake_submit
    )

    async def fake_analyze(**_kwargs):
        return {
            "registered": False,
            "material_id": None,
            "reason": "internal invoice",
        }

    monkeypatch.setattr(
        bot_main.api_client, "analyze_kb_material", fake_analyze, raising=False
    )

    response = TestClient(bot_app).post(
        "/telegram/webhook", json=_operator_kb_payload()
    )
    assert response.status_code == 200
    summary = [
        text for _, text in isolated_bot["dms"] if "Добавлено в базу" in text
    ][0]
    assert "📎 Добавлен в материалы" not in summary


def test_analyze_exception_is_silent_to_operator(
    isolated_bot, monkeypatch, caplog
) -> None:
    _patch_download(monkeypatch, isolated_bot["tmp_path"])

    async def fake_submit(**kwargs):
        return {
            "inserted_chunks": 4,
            "is_confidential": False,
            "deduplicated": False,
            "candidate_id": 11,
            "project_id": 7,
        }

    monkeypatch.setattr(
        bot_main.api_client, "submit_operator_upload", fake_submit
    )

    async def fake_analyze(**_kwargs):
        raise RuntimeError("api unreachable")

    monkeypatch.setattr(
        bot_main.api_client, "analyze_kb_material", fake_analyze, raising=False
    )

    with caplog.at_level("WARNING"):
        response = TestClient(bot_app).post(
            "/telegram/webhook", json=_operator_kb_payload()
        )
    assert response.status_code == 200
    summary = [
        text for _, text in isolated_bot["dms"] if "Добавлено в базу" in text
    ][0]
    # KB ack still sent; no materials line.
    assert "📎 Добавлен в материалы" not in summary
    assert any(
        r.message == "sales_kb_material_analyze_failed" for r in caplog.records
    )


def test_inline_text_kb_upload_skips_analyzer(
    isolated_bot, monkeypatch
) -> None:
    """``/kb_add <inline text>`` has no ``short_id`` → no analyzer call.

    Exercises the ``short_id is None`` branch in the hook.
    """

    async def fake_submit(**kwargs):
        return {
            "inserted_chunks": 1,
            "is_confidential": False,
            "deduplicated": False,
            "candidate_id": 11,
            "project_id": 7,
        }

    monkeypatch.setattr(
        bot_main.api_client, "submit_operator_upload", fake_submit
    )

    analyze_calls: list[dict[str, Any]] = []

    async def fake_analyze(**kwargs):
        analyze_calls.append(kwargs)
        return {"registered": True, "material_id": 1, "reason": "ok"}

    monkeypatch.setattr(
        bot_main.api_client, "analyze_kb_material", fake_analyze, raising=False
    )

    payload = {
        "update_id": 2,
        "message": {
            "message_id": 2,
            "chat": {"id": 100},
            "from": {"id": 200, "username": "ajdevy"},
            "text": "добавь в базу: офис работает 9-18",
        },
    }
    response = TestClient(bot_app).post("/telegram/webhook", json=payload)
    assert response.status_code == 200
    assert analyze_calls == []


def test_missing_project_id_skips_analyzer(
    isolated_bot, monkeypatch
) -> None:
    """If the api response omits ``project_id`` (e.g. a deduplicated row
    that predates project routing), the hook must NOT call the analyzer.

    Exercises the ``project_id is None`` branch in the hook.
    """
    _patch_download(monkeypatch, isolated_bot["tmp_path"])

    async def fake_submit(**kwargs):
        return {
            "inserted_chunks": 0,
            "is_confidential": False,
            "deduplicated": True,
            "candidate_id": 11,
            "project_id": None,
        }

    monkeypatch.setattr(
        bot_main.api_client, "submit_operator_upload", fake_submit
    )

    analyze_calls: list[dict[str, Any]] = []

    async def fake_analyze(**kwargs):
        analyze_calls.append(kwargs)
        return {"registered": True, "material_id": 1, "reason": "ok"}

    monkeypatch.setattr(
        bot_main.api_client, "analyze_kb_material", fake_analyze, raising=False
    )

    response = TestClient(bot_app).post(
        "/telegram/webhook", json=_operator_kb_payload()
    )
    assert response.status_code == 200
    assert analyze_calls == []


def test_missing_internal_token_skips_analyzer(
    isolated_bot, monkeypatch
) -> None:
    """If the bot has no service-token (mis-config), skip the analyzer.

    Exercises the ``not token`` guard in the hook.
    """
    monkeypatch.setattr(bot_main.settings, "internal_service_token", "")
    _patch_download(monkeypatch, isolated_bot["tmp_path"])

    async def fake_submit(**kwargs):
        return {
            "inserted_chunks": 1,
            "is_confidential": False,
            "deduplicated": False,
            "candidate_id": 11,
            "project_id": 7,
        }

    monkeypatch.setattr(
        bot_main.api_client, "submit_operator_upload", fake_submit
    )

    analyze_calls: list[dict[str, Any]] = []

    async def fake_analyze(**kwargs):
        analyze_calls.append(kwargs)
        return {"registered": True, "material_id": 1, "reason": "ok"}

    monkeypatch.setattr(
        bot_main.api_client, "analyze_kb_material", fake_analyze, raising=False
    )

    response = TestClient(bot_app).post(
        "/telegram/webhook", json=_operator_kb_payload()
    )
    assert response.status_code == 200
    assert analyze_calls == []


def test_registered_outcome_without_material_id_is_ignored(
    isolated_bot, monkeypatch
) -> None:
    """Defensive: if the api returns ``registered=True`` but no
    ``material_id``, the hook must NOT append a malformed line.
    """
    _patch_download(monkeypatch, isolated_bot["tmp_path"])

    async def fake_submit(**kwargs):
        return {
            "inserted_chunks": 1,
            "is_confidential": False,
            "deduplicated": False,
            "candidate_id": 11,
            "project_id": 7,
        }

    monkeypatch.setattr(
        bot_main.api_client, "submit_operator_upload", fake_submit
    )

    async def fake_analyze(**_kwargs):
        return {"registered": True, "material_id": None, "reason": "weird"}

    monkeypatch.setattr(
        bot_main.api_client, "analyze_kb_material", fake_analyze, raising=False
    )

    response = TestClient(bot_app).post(
        "/telegram/webhook", json=_operator_kb_payload()
    )
    assert response.status_code == 200
    summary = [
        text for _, text in isolated_bot["dms"] if "Добавлено в базу" in text
    ][0]
    assert "📎 Добавлен в материалы" not in summary


def test_confidential_upload_skips_analyzer_call(
    isolated_bot, monkeypatch
) -> None:
    _patch_download(monkeypatch, isolated_bot["tmp_path"])

    async def fake_submit(**kwargs):
        return {
            "inserted_chunks": 4,
            "is_confidential": True,
            "deduplicated": False,
            "candidate_id": 11,
            "project_id": 7,
        }

    monkeypatch.setattr(
        bot_main.api_client, "submit_operator_upload", fake_submit
    )

    analyze_calls: list[dict[str, Any]] = []

    async def fake_analyze(**kwargs):
        analyze_calls.append(kwargs)
        return {"registered": True, "material_id": 99, "reason": "ok"}

    monkeypatch.setattr(
        bot_main.api_client, "analyze_kb_material", fake_analyze, raising=False
    )

    response = TestClient(bot_app).post(
        "/telegram/webhook", json=_operator_kb_confidential_payload()
    )
    assert response.status_code == 200
    summary = [
        text for _, text in isolated_bot["dms"] if "Добавлено в базу" in text
    ][0]
    assert "📎 Добавлен в материалы" not in summary
    # Hook must never invoke the analyzer for a confidential file.
    assert analyze_calls == []
