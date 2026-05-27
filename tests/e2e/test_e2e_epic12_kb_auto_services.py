"""Epic 12 Story 12.05c — end-to-end KB-upload → services auto-extraction.

Drives the full bot→api round trip through ``TestClient``:

1. The operator sends ``/kb_add`` with a fixture catalog PDF. The LLM
   (stubbed via ``openrouter_client.complete_json``) returns three
   services; the bot's KB-upload ack carries the services line
   ``📦 Услуги добавлены: ...``. Three rows land in ``project_services``.
2. The operator sends ``/kb_add`` with a second catalog that names two
   services — one new, one already present. Only the new row is
   inserted; the ack mentions only the new one.
3. The operator sends ``/kb_add confidential`` with a tour-shaped PDF.
   Neither the materials nor the services hook is invoked (LLM is never
   reached); no services row is written.
4. The operator sends ``/kb_add`` with a non-offerings file. The LLM
   returns ``services: []``; the ack has no ``📦`` line and the
   services table stays unchanged.

The bot's ``api_client`` HTTP calls are re-routed through ``TestClient``
so the real ``/knowledge/operator_upload``,
``/sales/materials/analyze-kb-file``, and
``/sales/services/extract-from-kb-file`` endpoints answer inside this
test process. Only the LLM call is stubbed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.calendar.project_services_repository import (
    ProjectServiceRepository,
)
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
    pytest.mark.story("12-05c"),
]

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "sales"
_TOUR_CATALOG = _FIXTURE_DIR / "tour_catalog.pdf"
_INTERNAL_INVOICE = _FIXTURE_DIR / "internal_invoice.pdf"

_INTERNAL_TOKEN = "e2e-internal-token"
_OPERATOR_USERNAME = "ajdevy"
_CHAT_ID = 555_001
# The api resolves the upload to the default project (id=1) since the
# operator is the only seeded operator.
_PROJECT_ID = 1


class _StubHitlRepo:
    def get_runtime_config(self, key: str) -> str | None:
        return None

    def set_runtime_config(self, **_: Any) -> None:  # pragma: no cover
        return None

    def list_all(self) -> list:  # pragma: no cover
        return []


def _count_services(calendar_db_path: Path) -> int:
    if not calendar_db_path.exists():
        return 0
    with sqlite3.connect(calendar_db_path) as connection:
        cursor = connection.execute(
            "SELECT COUNT(*) FROM project_services WHERE project_id = ?",
            (_PROJECT_ID,),
        )
        return int(cursor.fetchone()[0])


def _service_names(calendar_db_path: Path) -> list[str]:
    if not calendar_db_path.exists():
        return []
    with sqlite3.connect(calendar_db_path) as connection:
        rows = connection.execute(
            "SELECT name FROM project_services WHERE project_id = ? "
            "ORDER BY id",
            (_PROJECT_ID,),
        ).fetchall()
    return [str(row[0]) for row in rows]


@pytest.fixture
def shared_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "operator_files_db": tmp_path / "operator_files.db",
        "knowledge_db": tmp_path / "knowledge.db",
        "rag_db": tmp_path / "rag.db",
        "sales_db": tmp_path / "sales.db",
        "calendar_db": tmp_path / "calendar.db",
        "hitl_db": tmp_path / "hitl.db",
        "answer_trace_db": tmp_path / "answer_traces.db",
        "incidents_db": tmp_path / "incidents.db",
        "persistence_db": tmp_path / "story.db",
        "uploads_dir": tmp_path / "uploads",
        "tmp_root": tmp_path,
    }


@pytest.fixture
def wired_stack(monkeypatch: pytest.MonkeyPatch, shared_paths: dict[str, Path]):
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
    monkeypatch.setattr(
        api_main.settings, "calendar_db_path", str(paths["calendar_db"])
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

    # Rebind the project_services_repository to the tmp calendar DB — the
    # extractor adapter uses this repo for both find_by_name + upsert.
    fresh_services_repo = ProjectServiceRepository(db_path=str(paths["calendar_db"]))
    monkeypatch.setattr(
        api_main, "project_services_repository", fresh_services_repo
    )
    # The adapter inside the services_extractor holds the original repo,
    # so we rebuild the adapter to point at the fresh repo.
    api_main.services_extractor._repo = api_main._ServicesExtractorRepoAdapter(
        repo=fresh_services_repo
    )

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

    # Queue LLM responses; each call pops one off the front. Both 12.05b
    # and 12.05c hooks call the LLM per upload (when non-confidential).
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

    async def fake_extract_kb_services(
        *,
        project_id: int,
        operator_file_short_id: str,
        internal_token: str,
    ) -> dict:
        response = api_tc.post(
            "/sales/services/extract-from-kb-file",
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
    monkeypatch.setattr(
        bot_main.api_client,
        "extract_kb_services",
        fake_extract_kb_services,
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


def test_first_upload_extracts_three_services(wired_stack) -> None:
    paths = wired_stack["paths"]
    wired_stack["patch_downloader"](_TOUR_CATALOG)
    # Two LLM calls per upload: materials then services. Each upload
    # triggers an asyncio.gather; ordering between them isn't guaranteed
    # — but our fake just pops two responses from the queue regardless.
    wired_stack["queue_llm"](
        {
            "sendable": False,
            "reason": "not a sendable doc",
            "suggested_kind": "pdf",
            "suggested_caption": None,
        }
    )
    wired_stack["queue_llm"](
        {
            "services": [
                {"name": "Медовеевка Лайт", "description": "Лёгкий маршрут."},
                {"name": "Каньонинг", "description": None},
                {"name": "Ивановский водопад", "description": None},
            ],
            "reason": "tour catalog",
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

    summary = [
        text for _, text in wired_stack["sent_dms"] if "Добавлено в базу" in text
    ][0]
    assert (
        "📦 Услуги добавлены: Медовеевка Лайт, Каньонинг, Ивановский водопад."
        in summary
    )
    assert _count_services(paths["calendar_db"]) == 3
    names = _service_names(paths["calendar_db"])
    assert set(names) == {"Медовеевка Лайт", "Каньонинг", "Ивановский водопад"}


def test_second_upload_with_one_new_one_existing_only_adds_new(
    wired_stack,
) -> None:
    paths = wired_stack["paths"]
    wired_stack["patch_downloader"](_TOUR_CATALOG)

    # First upload: three services.
    wired_stack["queue_llm"](
        {
            "sendable": False,
            "reason": "x",
            "suggested_kind": "pdf",
            "suggested_caption": None,
        }
    )
    wired_stack["queue_llm"](
        {
            "services": [
                {"name": "Медовеевка Лайт", "description": "manual desc"},
                {"name": "Каньонинг", "description": None},
                {"name": "Ивановский водопад", "description": None},
            ],
            "reason": "tour catalog",
        }
    )
    TestClient(bot_app).post(
        "/telegram/webhook",
        json=_kb_upload_payload(
            file_name="tour_catalog.pdf",
            file_id="TG_FILE_TOUR_A",
            caption="/kb_add",
            update_id=1,
            message_id=1,
        ),
    )
    assert _count_services(paths["calendar_db"]) == 3

    # Second upload: LLM returns two services — one new, one already
    # present. Only the new one is added; the ack lists only the new.
    wired_stack["queue_llm"](
        {
            "sendable": False,
            "reason": "x",
            "suggested_kind": "pdf",
            "suggested_caption": None,
        }
    )
    wired_stack["queue_llm"](
        {
            "services": [
                {"name": "Каньонинг", "description": "auto extracted again"},
                {"name": "Багги-тур", "description": "Тур на квадроциклах."},
            ],
            "reason": "tour catalog v2",
        }
    )
    TestClient(bot_app).post(
        "/telegram/webhook",
        json=_kb_upload_payload(
            file_name="tour_catalog.pdf",
            file_id="TG_FILE_TOUR_B",
            caption="/kb_add",
            update_id=2,
            message_id=2,
        ),
    )

    assert _count_services(paths["calendar_db"]) == 4
    second_summary = [
        text for _, text in wired_stack["sent_dms"]
        if "Добавлено в базу" in text
    ][-1]
    assert "📦 Услуги добавлены: Багги-тур." in second_summary
    # Каньонинг already exists — its description must NOT be overwritten.
    # The api repository uses a Unicode-aware ``lower`` UDF that is not
    # available on a raw sqlite3 connection, so match by exact name here.
    with sqlite3.connect(paths["calendar_db"]) as connection:
        row = connection.execute(
            "SELECT description FROM project_services "
            "WHERE project_id = ? AND name = ?",
            (_PROJECT_ID, "Каньонинг"),
        ).fetchone()
    assert row is not None
    # The original (None) was preserved — auto-extracted desc on re-upload
    # never overwrites.
    assert row[0] is None


def test_confidential_upload_never_adds_services_row(wired_stack) -> None:
    paths = wired_stack["paths"]
    wired_stack["patch_downloader"](_TOUR_CATALOG)
    # No LLM responses queued — if either hook ever calls the LLM the
    # fake will raise AssertionError.

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

    summary = [
        text for _, text in wired_stack["sent_dms"] if "Добавлено в базу" in text
    ][0]
    assert "📦 Услуги добавлены" not in summary
    assert _count_services(paths["calendar_db"]) == 0
    assert wired_stack["llm_calls"] == []


def test_non_service_file_leaves_no_row(wired_stack) -> None:
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
    wired_stack["queue_llm"](
        {"services": [], "reason": "счёт-фактура"}
    )

    response = TestClient(bot_app).post(
        "/telegram/webhook",
        json=_kb_upload_payload(
            file_name="internal_invoice.pdf",
            file_id="TG_FILE_INV",
            caption="/kb_add",
            update_id=4,
            message_id=4,
        ),
    )
    assert response.status_code == 200

    summary = [
        text for _, text in wired_stack["sent_dms"] if "Добавлено в базу" in text
    ][0]
    assert "📦 Услуги добавлены" not in summary
    assert _count_services(paths["calendar_db"]) == 0
