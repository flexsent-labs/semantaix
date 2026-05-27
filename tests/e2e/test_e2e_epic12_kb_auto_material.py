"""Epic 12 Story 12.05b — end-to-end KB-upload → client-materials auto-promotion.

Drives the full bot→api round trip through ``TestClient``:

1. The operator sends ``/kb_add`` with ``tour_catalog.pdf``. The LLM
   (stubbed via ``openrouter_client.complete_json``) returns
   ``sendable=true`` and the bot's KB-upload ack carries the materials
   line ``📎 Добавлен в материалы для клиентов (id=<n>).``. A
   ``client_materials`` row is written to the shared sales DB.
2. The operator sends ``/kb_add`` with ``internal_invoice.pdf``. The
   stubbed LLM returns ``sendable=false``; the ack has no extra line and
   the ``client_materials`` table stays empty.
3. The operator sends ``/kb_add confidential`` with a sendable-looking
   PDF. The hook short-circuits (the LLM is never reached) and no row
   is written — confidential KB files must never become customer-facing
   materials.

The bot's ``api_client`` HTTP calls are re-routed through ``TestClient``
so the real ``/knowledge/operator_upload`` and
``/sales/materials/analyze-kb-file`` endpoints run inside this test
process. Only the LLM call is stubbed; everything else (extraction,
operator_files row, knowledge candidate row, ATTACH read in
``operator_files_view``, repository writes) executes for real.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.main import app as api_app
from services.api.app.sales.client_materials_repository import (
    init_schema as init_client_materials_schema,
)
from services.bot_gateway.app import main as bot_main
from services.bot_gateway.app.kb_session import OperatorKbSessionRepository
from services.bot_gateway.app.main import app as bot_app
from services.bot_gateway.app.media_group_buffer import MediaGroupBuffer
from services.bot_gateway.app.operator_files import OperatorFileRepository
from services.bot_gateway.app.telegram_file_download import DownloadedFile

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.epic("12"),
    pytest.mark.story("12-05b"),
]

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "sales"
_TOUR_CATALOG = _FIXTURE_DIR / "tour_catalog.pdf"
_INTERNAL_INVOICE = _FIXTURE_DIR / "internal_invoice.pdf"

_INTERNAL_TOKEN = "e2e-internal-token"
_OPERATOR_USERNAME = "ajdevy"
_CHAT_ID = 555_001


class _StubHitlRepo:
    def get_runtime_config(self, key: str) -> str | None:
        return None

    def set_runtime_config(self, **_: Any) -> None:  # pragma: no cover
        return None

    def list_all(self) -> list:  # pragma: no cover
        return []


def _count_client_materials(sales_db_path: Path) -> int:
    if not sales_db_path.exists():
        return 0
    with sqlite3.connect(sales_db_path) as connection:
        cursor = connection.execute("SELECT COUNT(*) FROM client_materials")
        return int(cursor.fetchone()[0])


def _count_client_materials_for_path(sales_db_path: Path, local_path: str) -> int:
    if not sales_db_path.exists():
        return 0
    with sqlite3.connect(sales_db_path) as connection:
        cursor = connection.execute(
            "SELECT COUNT(*) FROM client_materials WHERE local_path = ?",
            (local_path,),
        )
        return int(cursor.fetchone()[0])


@pytest.fixture
def shared_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "operator_files_db": tmp_path / "operator_files.db",
        "knowledge_db": tmp_path / "knowledge.db",
        "rag_db": tmp_path / "rag.db",
        "sales_db": tmp_path / "sales.db",
        "hitl_db": tmp_path / "hitl.db",
        "answer_trace_db": tmp_path / "answer_traces.db",
        "incidents_db": tmp_path / "incidents.db",
        "persistence_db": tmp_path / "story.db",
        "uploads_dir": tmp_path / "uploads",
        "tmp_root": tmp_path,
    }


@pytest.fixture
def wired_stack(monkeypatch: pytest.MonkeyPatch, shared_paths: dict[str, Path]):
    """Wire bot + api against shared tmp DBs and route ``api_client`` calls
    through the api ``TestClient`` so the integration is fully real."""
    paths = shared_paths
    paths["uploads_dir"].mkdir(parents=True, exist_ok=True)

    # api: rebind every repository to tmp paths so a clean DB seeds each test.
    monkeypatch.setattr(
        api_main.settings, "operator_files_db_path", str(paths["operator_files_db"])
    )
    monkeypatch.setattr(
        api_main.settings, "knowledge_db_path", str(paths["knowledge_db"])
    )
    monkeypatch.setattr(
        api_main.settings, "sales_db_path", str(paths["sales_db"])
    )
    monkeypatch.setattr(api_main.settings, "rag_db_path", str(paths["rag_db"]))
    monkeypatch.setattr(
        api_main.settings, "internal_service_token", _INTERNAL_TOKEN
    )

    monkeypatch.setattr(
        api_main.operator_files_view,
        "operator_files_db_path",
        str(paths["operator_files_db"]),
    )
    monkeypatch.setattr(
        api_main.operator_files_view,
        "knowledge_db_path",
        str(paths["knowledge_db"]),
    )
    api_main.knowledge_moderation_repository.db_path = str(paths["knowledge_db"])
    api_main.rag_repository.db_path = str(paths["rag_db"])
    api_main.client_materials_repository.db_path = str(paths["sales_db"])
    init_client_materials_schema(str(paths["sales_db"]))
    api_main.incident_repository.db_path = str(paths["incidents_db"])
    api_main.hitl_ticket_repository.db_path = str(paths["hitl_db"])
    api_main.answer_trace_repository.db_path = str(paths["answer_trace_db"])

    # bot: share the operator_files DB with the api.
    monkeypatch.setattr(
        bot_main.settings, "persistence_db_path", str(paths["persistence_db"])
    )
    monkeypatch.setattr(
        bot_main.settings, "hitl_ticket_db_path", str(paths["hitl_db"])
    )
    monkeypatch.setattr(
        bot_main.settings,
        "operator_files_db_path",
        str(paths["operator_files_db"]),
    )
    monkeypatch.setattr(
        bot_main.settings, "operator_upload_storage_dir", str(paths["uploads_dir"])
    )
    monkeypatch.setattr(bot_main.settings, "operator_upload_max_bytes", 5 * 1024 * 1024)
    monkeypatch.setattr(bot_main.settings, "telegram_bot_token", "TKN")
    monkeypatch.setattr(
        bot_main.settings, "hitl_primary_operator_username", f"@{_OPERATOR_USERNAME}"
    )
    monkeypatch.setattr(
        bot_main.settings, "internal_service_token", _INTERNAL_TOKEN
    )
    monkeypatch.setattr(bot_main, "hitl_ticket_repository", _StubHitlRepo())
    monkeypatch.setattr(
        bot_main,
        "operator_file_repository",
        OperatorFileRepository(str(paths["operator_files_db"])),
    )
    monkeypatch.setattr(
        bot_main,
        "kb_session_repository",
        OperatorKbSessionRepository(str(paths["hitl_db"])),
    )
    monkeypatch.setattr(
        bot_main, "media_group_buffer", MediaGroupBuffer(str(paths["hitl_db"]))
    )
    monkeypatch.setattr(
        bot_main.settings, "operator_media_group_debounce_seconds", 0
    )

    sent_dms: list[tuple[int, str]] = []

    async def fake_send_dm(chat_id: int, text: str) -> None:
        sent_dms.append((chat_id, text))

    monkeypatch.setattr(bot_main, "_send_dm", fake_send_dm)

    # Capture LLM payloads so the test can pre-stage verdicts.
    llm_responses: list[dict[str, Any]] = []
    llm_calls: list[dict[str, Any]] = []

    async def fake_complete_json(*, system: str, user: str, model: str | None = None):
        llm_calls.append({"system": system, "user": user, "model": model})
        if not llm_responses:
            raise AssertionError("LLM called without a queued response")
        return llm_responses.pop(0)

    monkeypatch.setattr(
        api_main.openrouter_client,
        "complete_json",
        AsyncMock(side_effect=fake_complete_json),
    )

    # Re-route the bot's api_client through the api TestClient so the real
    # endpoints answer the bot's calls.
    api_tc = TestClient(api_app)

    async def fake_submit_operator_upload(
        *,
        operator_username: str,
        source_file_type: str,
        source_file_name: str | None,
        stored_binary_path: str | None,
        is_confidential: bool,
        inline_text: str | None = None,
        operator_short_id: str | None = None,
        timeout_seconds: int | None = None,
    ) -> dict:
        response = api_tc.post(
            "/knowledge/operator_upload",
            json={
                "operator_username": operator_username,
                "source_file_type": source_file_type,
                "source_file_name": source_file_name,
                "stored_binary_path": stored_binary_path,
                "is_confidential": is_confidential,
                "inline_text": inline_text,
                "operator_short_id": operator_short_id,
            },
        )
        response.raise_for_status()
        return response.json()

    async def fake_analyze_kb_material(
        *,
        project_id: int,
        operator_file_short_id: str,
        internal_token: str,
    ) -> dict:
        response = api_tc.post(
            "/sales/materials/analyze-kb-file",
            json={
                "project_id": project_id,
                "operator_file_short_id": operator_file_short_id,
            },
            headers={"Authorization": f"Bearer {internal_token}"},
        )
        response.raise_for_status()
        return response.json()

    monkeypatch.setattr(
        bot_main.api_client,
        "submit_operator_upload",
        fake_submit_operator_upload,
        raising=False,
    )
    monkeypatch.setattr(
        bot_main.api_client,
        "analyze_kb_material",
        fake_analyze_kb_material,
        raising=False,
    )

    def patch_downloader(local_path: Path, mime_type: str = "application/pdf") -> None:
        async def fake_download(self, *, file_id, suggested_extension, mime_type=None):
            return DownloadedFile(
                path=local_path, byte_size=local_path.stat().st_size, mime_type=mime_type
            )

        monkeypatch.setattr(
            bot_main.TelegramFileDownloader, "download", fake_download, raising=False
        )

    return {
        "paths": paths,
        "sent_dms": sent_dms,
        "queue_llm": llm_responses.append,
        "llm_calls": llm_calls,
        "patch_downloader": patch_downloader,
    }


def _kb_upload_payload(
    *, file_name: str, file_id: str, caption: str, update_id: int, message_id: int
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "chat": {"id": _CHAT_ID},
            "from": {"id": 200, "username": _OPERATOR_USERNAME},
            "caption": caption,
            "document": {
                "file_id": file_id,
                "file_name": file_name,
                "mime_type": "application/pdf",
                "file_size": 1024,
            },
        },
    }


def test_sendable_pdf_appends_materials_line_and_registers_row(wired_stack) -> None:
    paths = wired_stack["paths"]
    wired_stack["patch_downloader"](_TOUR_CATALOG)
    wired_stack["queue_llm"](
        {
            "sendable": True,
            "reason": "tour catalog promotional content",
            "suggested_kind": "pdf",
            "suggested_caption": "Каталог туров",
        }
    )

    response = TestClient(bot_app).post(
        "/telegram/webhook",
        json=_kb_upload_payload(
            file_name="tour_catalog.pdf",
            file_id="TG_FILE_TOUR",
            caption="/kb_add",
            update_id=1,
            message_id=1,
        ),
    )
    assert response.status_code == 200

    ack_lines = [
        text for _, text in wired_stack["sent_dms"] if "Добавлено в базу" in text
    ]
    assert ack_lines, wired_stack["sent_dms"]
    ack = ack_lines[0]
    assert "📎 Добавлен в материалы для клиентов (id=" in ack
    assert ack.rstrip().endswith(").")

    # One LLM call (analyzer).
    assert len(wired_stack["llm_calls"]) == 1, wired_stack["llm_calls"]

    # client_materials row exists and points at the same local_path.
    assert (
        _count_client_materials_for_path(paths["sales_db"], str(_TOUR_CATALOG))
        == 1
    )


def test_non_sendable_pdf_leaves_no_row_and_no_extra_ack_line(wired_stack) -> None:
    paths = wired_stack["paths"]
    wired_stack["patch_downloader"](_INTERNAL_INVOICE)
    wired_stack["queue_llm"](
        {
            "sendable": False,
            "reason": "internal invoice",
            "suggested_kind": "pdf",
            "suggested_caption": None,
        }
    )

    response = TestClient(bot_app).post(
        "/telegram/webhook",
        json=_kb_upload_payload(
            file_name="internal_invoice.pdf",
            file_id="TG_FILE_INV",
            caption="/kb_add",
            update_id=2,
            message_id=2,
        ),
    )
    assert response.status_code == 200

    ack_lines = [
        text for _, text in wired_stack["sent_dms"] if "Добавлено в базу" in text
    ]
    assert ack_lines, wired_stack["sent_dms"]
    ack = ack_lines[0]
    assert "📎 Добавлен в материалы" not in ack
    # LLM was consulted (non-confidential branch) but no row was written.
    assert len(wired_stack["llm_calls"]) == 1
    assert _count_client_materials(paths["sales_db"]) == 0


def test_confidential_kb_upload_never_creates_material(wired_stack) -> None:
    """/kb_add confidential of a sendable-looking PDF must never register a
    client_materials row — the bot hook skips the analyzer call entirely and
    the api enforces the same rule server-side as defense in depth."""
    paths = wired_stack["paths"]
    wired_stack["patch_downloader"](_TOUR_CATALOG)
    # If the LLM is ever called, the test should fail loudly — the queue is
    # intentionally empty so any call would raise AssertionError.

    response = TestClient(bot_app).post(
        "/telegram/webhook",
        json=_kb_upload_payload(
            file_name="tour_catalog.pdf",
            file_id="TG_FILE_TOUR_CONF",
            caption="/kb_add confidential",
            update_id=3,
            message_id=3,
        ),
    )
    assert response.status_code == 200

    ack_lines = [
        text for _, text in wired_stack["sent_dms"] if "Добавлено в базу" in text
    ]
    assert ack_lines, wired_stack["sent_dms"]
    ack = ack_lines[0]
    assert "📎 Добавлен в материалы" not in ack
    assert wired_stack["llm_calls"] == []
    assert _count_client_materials(paths["sales_db"]) == 0
