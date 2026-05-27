"""Epic 12 Story 12.05 — autonomous client materials dispatch E2E.

Covers the three spec-mandated exit criteria for the ``/material`` flow:

1. **Operator registration** — the bot's ``/telegram/webhook`` receives a
   ``/material`` reply-to-video update; the bot downloads the binary into
   ``<sales_materials_storage_dir>/<project_id>/`` and posts to the api
   ``POST /sales/materials`` endpoint. A ``client_materials`` row is
   written under the right ``project_id``.
2. **Scoping-completion dispatch with first-send file_id caching** — the
   first customer finishes scoping (5/5 fields). The
   ``SalesPersonaAnswerer`` fires the ``tour_preview`` media moment via
   the in-process material dispatcher, which calls
   ``telegram_bot_sender.send_video`` with ``local_path`` (the file is
   read off disk exactly once). Telegram returns a freshly assigned
   ``telegram_file_id`` and the api caches it on the row.
3. **Second customer reuses the cached file_id** — a second customer
   (different chat) reaches the same moment. The dispatcher now sends
   via the cached ``file_id``; the file is NOT read off disk again. The
   spy on the disk-read seam stays at one observation.

The test uses real api routes via ``TestClient(api_app)`` and routes the
bot's ``api_client`` calls through that same client, so the
``/sales/materials`` round-trip is the live endpoint. The
``telegram_bot_sender`` and the bot's ``TelegramFileDownloader`` are the
only stubbed seams.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.answerers import AnswerContext
from services.api.app.main import app as api_app
from services.api.app.sales.client_materials_repository import (
    ClientMaterialsRepository,
)
from services.api.app.sales.state_repository import StateRepository
from services.bot_gateway.app import main as bot_main
from services.bot_gateway.app.main import app as bot_app
from services.bot_gateway.app.telegram_file_download import DownloadedFile

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.epic("12"),
    pytest.mark.story("12-05"),
]

_NOW = datetime(2026, 5, 27, 10, 0, tzinfo=UTC)
_INTERNAL_TOKEN = "e2e-internal-token"
_OPERATOR_USERNAME = "ajdevy"
_OPERATOR_CHAT_ID = 555_999
_PROJECT_ID = 1


class _StubHitlRepo:
    """Bot-side hitl_ticket_repository stub; the /material flow never reads it."""

    def get_runtime_config(self, key: str) -> str | None:
        return None

    def set_runtime_config(self, **_: Any) -> None:  # pragma: no cover
        return None

    def list_all(self) -> list:  # pragma: no cover
        return []


class _SpySender:
    """Stand-in for ``api_main.telegram_bot_sender``.

    Tracks every ``send_video`` call with the ``local_path`` argument so the
    test can prove a fresh upload reads disk on the first send and the
    cached ``file_id`` path skips disk on the second.
    """

    def __init__(self) -> None:
        self.disk_reads: list[Path] = []
        self.calls: list[dict[str, Any]] = []
        self._next_file_id = "FRESH-TG-VID"

    async def send_message(self, *, chat_id: int, text: str) -> int:
        # /conversations/inbound calls send_message to deliver the textual
        # pitch — return a synthetic message_id so the answer trace lands.
        return 1_000_000 + chat_id

    async def send_video(
        self,
        *,
        chat_id: int,
        file_id: str | None = None,
        local_path: Path | None = None,
        caption: str | None = None,
    ) -> dict[str, Any]:
        call: dict[str, Any] = {
            "chat_id": chat_id,
            "file_id": file_id,
            "local_path": local_path,
            "caption": caption,
        }
        self.calls.append(call)
        if file_id is None and local_path is not None:
            self.disk_reads.append(local_path)
            return {"ok": True, "telegram_file_id": self._next_file_id}
        return {"ok": True, "telegram_file_id": file_id}

    async def send_photo(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    async def send_document(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError


def _count_client_materials(sales_db: Path) -> int:
    with sqlite3.connect(sales_db) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM client_materials").fetchone()[0])


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Wire bot+api against tmp DBs; route bot api_client through api TestClient."""
    paths = {
        "sales_db": tmp_path / "sales.db",
        "operator_files_db": tmp_path / "operator_files.db",
        "projects_db": tmp_path / "projects.db",
        "operators_db": tmp_path / "operators.db",
        "hitl_db": tmp_path / "hitl.db",
        "persistence_db": tmp_path / "story.db",
        "storage_root": tmp_path / "sales_materials",
        "uploads_dir": tmp_path / "uploads",
    }
    paths["storage_root"].mkdir(parents=True, exist_ok=True)
    paths["uploads_dir"].mkdir(parents=True, exist_ok=True)

    # api side: rebind shared repos to tmp DBs.
    monkeypatch.setattr(api_main.settings, "sales_db_path", str(paths["sales_db"]))
    monkeypatch.setattr(api_main.settings, "internal_service_token", _INTERNAL_TOKEN)
    # Sales persona answerer's state/selector/dispatcher all live on the
    # module-level singletons. Use monkeypatch.setattr so the original db_path
    # is restored after the test (avoids polluting later tests that share
    # these singletons).
    monkeypatch.setattr(
        api_main.client_materials_repository, "db_path", str(paths["sales_db"])
    )
    monkeypatch.setattr(
        api_main.sales_state_repository, "db_path", str(paths["sales_db"])
    )
    monkeypatch.setattr(
        api_main.sales_services_repository, "db_path", str(paths["sales_db"])
    )
    monkeypatch.setattr(
        api_main.sales_followup_repository, "db_path", str(paths["sales_db"])
    )
    # Re-init schema on the fresh sales DB so the round-trip writes/reads work.
    from services.api.app.sales.client_materials_repository import (
        init_schema as init_cm_schema,
    )
    from services.api.app.sales.state_repository import (
        init_schema as init_state_schema,
    )

    init_cm_schema(str(paths["sales_db"]))
    init_state_schema(str(paths["sales_db"]))

    # Spy sender on the api side. The in-process dispatcher reads
    # ``api_main.telegram_bot_sender`` directly, so a single setattr is enough.
    sender = _SpySender()
    monkeypatch.setattr(api_main, "telegram_bot_sender", sender)

    # bot side: shared settings + tmp DBs.
    monkeypatch.setattr(
        bot_main.settings, "persistence_db_path", str(paths["persistence_db"])
    )
    monkeypatch.setattr(
        bot_main.settings, "operator_files_db_path", str(paths["operator_files_db"])
    )
    monkeypatch.setattr(bot_main.settings, "hitl_ticket_db_path", str(paths["hitl_db"]))
    monkeypatch.setattr(bot_main.settings, "telegram_bot_token", "TKN")
    monkeypatch.setattr(
        bot_main.settings, "hitl_primary_operator_username", f"@{_OPERATOR_USERNAME}"
    )
    monkeypatch.setattr(
        bot_main.settings, "hitl_config_admin_username", f"@{_OPERATOR_USERNAME}"
    )
    monkeypatch.setattr(
        bot_main.settings, "internal_service_token", _INTERNAL_TOKEN
    )
    monkeypatch.setattr(
        bot_main.settings,
        "sales_materials_storage_dir",
        str(paths["storage_root"]),
    )
    monkeypatch.setattr(
        bot_main.settings, "operator_upload_storage_dir", str(paths["uploads_dir"])
    )
    monkeypatch.setattr(bot_main, "hitl_ticket_repository", _StubHitlRepo())

    # Capture DMs the bot sends out so the test can introspect them.
    sent_dms: list[tuple[int, str]] = []

    async def fake_send_dm(chat_id: int, text: str) -> None:
        sent_dms.append((chat_id, text))

    monkeypatch.setattr(bot_main, "_send_dm", fake_send_dm)

    # Route bot api_client.* calls through the api TestClient so the
    # /material command runs against the real /sales/materials endpoint.
    api_tc = TestClient(api_app)

    async def fake_find_operator_by_username(*, username: str) -> dict | None:
        resp = api_tc.get(f"/operators/by-username/{username}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def fake_add_sales_material(
        *,
        project_id: int,
        kind: str,
        local_path: str,
        byte_size: int,
        internal_token: str,
        duration_seconds: int | None = None,
        caption: str | None = None,
        tags: list[str] | None = None,
        telegram_file_id: str | None = None,
        source_operator_file_id: str | None = None,
    ) -> dict:
        resp = api_tc.post(
            "/sales/materials",
            headers={"Authorization": f"Bearer {internal_token}"},
            json={
                "project_id": project_id,
                "kind": kind,
                "local_path": local_path,
                "byte_size": byte_size,
                "duration_seconds": duration_seconds,
                "caption": caption,
                "tags": tags,
                "telegram_file_id": telegram_file_id,
                "source_operator_file_id": source_operator_file_id,
            },
        )
        resp.raise_for_status()
        return resp.json()

    monkeypatch.setattr(
        bot_main.api_client,
        "find_operator_by_username",
        AsyncMock(side_effect=fake_find_operator_by_username),
        raising=False,
    )
    monkeypatch.setattr(
        bot_main.api_client,
        "add_sales_material",
        AsyncMock(side_effect=fake_add_sales_material),
        raising=False,
    )

    # Seed: a default project + a registered operator so the bot's
    # operator-resolver and the api's project resolver both find rows.
    monkeypatch.setattr(
        api_main.project_repository, "db_path", str(paths["projects_db"])
    )
    api_main.project_repository.init_schema()
    monkeypatch.setattr(
        api_main.operator_repository, "db_path", str(paths["operators_db"])
    )
    api_main.operator_repository.init_schema()
    project = api_main.project_repository.ensure_default_project()
    api_main.operator_repository.ensure_default_operator(
        username=f"@{_OPERATOR_USERNAME}",
        project_id=project.id,
        chat_id=_OPERATOR_CHAT_ID,
    )

    # Stub the bot's TelegramFileDownloader so /material's reply-to-video
    # branch lands a real file under the per-project storage subdir.
    expected_storage_dir = paths["storage_root"] / str(project.id)

    download_calls: list[Path] = []
    landed_paths: list[Path] = []

    async def fake_download(
        self,  # the downloader instance (storage_dir-aware factory)
        *,
        file_id: str,
        suggested_extension: str,
        mime_type: str | None = None,
    ) -> DownloadedFile:
        # Confirm the factory passed the per-project dir into the downloader.
        download_calls.append(self._storage_dir)
        target_dir = Path(self._storage_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        dst = target_dir / "vid.mp4"
        dst.write_bytes(b"mp4-bytes")
        landed_paths.append(dst)
        return DownloadedFile(
            path=dst, byte_size=dst.stat().st_size, mime_type=mime_type
        )

    monkeypatch.setattr(
        bot_main.TelegramFileDownloader, "download", fake_download, raising=False
    )

    return {
        "paths": paths,
        "project_id": project.id,
        "sender": sender,
        "sent_dms": sent_dms,
        "expected_storage_dir": expected_storage_dir,
        "download_calls": download_calls,
        "landed_paths": landed_paths,
        "monkeypatch": monkeypatch,
    }


def _material_register_payload() -> dict[str, Any]:
    """Telegram webhook payload: ``/material`` reply to a video message."""
    return {
        "update_id": 1,
        "message": {
            "message_id": 11,
            "chat": {"id": _OPERATOR_CHAT_ID},
            "from": {"id": 1, "username": _OPERATOR_USERNAME},
            "text": "/material Тур-превью",
            "reply_to_message": {
                "message_id": 10,
                "chat": {"id": _OPERATOR_CHAT_ID},
                "from": {"id": 1, "username": _OPERATOR_USERNAME},
                "video": {
                    "file_id": "TG-VID-ORIG",
                    "file_size": 9,
                    "mime_type": "video/mp4",
                },
            },
        },
    }


@pytest.mark.asyncio
async def test_material_dispatch_full_flow(stack: dict[str, Any]) -> None:
    project_id = stack["project_id"]
    sender: _SpySender = stack["sender"]

    # ---- Step 1: operator registers the material via /material -----------
    resp = TestClient(bot_app).post(
        "/telegram/webhook", json=_material_register_payload()
    )
    assert resp.status_code == 200, resp.text
    # The file landed under <storage_root>/<project_id>/ (spec exit criterion).
    assert stack["download_calls"] == [stack["expected_storage_dir"]]
    assert stack["landed_paths"], "expected the fake downloader to write a file"
    assert (
        stack["landed_paths"][0].parent == stack["expected_storage_dir"]
    ), stack["landed_paths"]
    # One client_materials row exists for the project.
    assert _count_client_materials(stack["paths"]["sales_db"]) == 1
    materials_repo = ClientMaterialsRepository(db_path=str(stack["paths"]["sales_db"]))
    rows = materials_repo.list_active(project_id=project_id)
    assert len(rows) == 1
    material_row = rows[0]
    assert material_row.kind == "video"
    assert material_row.caption == "Тур-превью"
    assert material_row.telegram_file_id == "TG-VID-ORIG"  # pre-cached on register

    # Add a tag so the selector finds it via purpose="tour_preview".
    with sqlite3.connect(stack["paths"]["sales_db"]) as conn:
        conn.execute(
            "UPDATE client_materials SET tags_json = ?, telegram_file_id = NULL "
            "WHERE id = ?",
            ('["tour_preview"]', material_row.id),
        )
        conn.commit()

    # ---- Step 2: first customer completes scoping → tour_preview dispatch ----
    state_repo = StateRepository(db_path=str(stack["paths"]["sales_db"]))
    chat_id_1 = 901
    state_repo.upsert(
        chat_id=chat_id_1,
        project_id=project_id,
        current_stage="scoping",
        collected_intent={
            "dates": "1 мая",
            "headcount": 4,
            "vehicle_count": 2,
            "difficulty": "начальный",
            "drivers": None,
        },
        now=_NOW,
        last_bot_msg_at=_NOW,
    )

    class _StubOpenRouter:
        def __init__(self, payloads: list[dict[str, Any]]):
            self._payloads = payloads

        async def complete_json(
            self, *, system: str, user: str, model: str | None = None
        ) -> dict[str, Any]:
            return self._payloads.pop(0)

    answerer = api_main.sales_persona_answerer
    # Swap in a stub openrouter + clock that returns the seeded "now".
    # monkeypatch.setattr restores the originals after the test so the
    # module-level singleton is not polluted for downstream tests.
    stub_openrouter = _StubOpenRouter(
        [
            {
                "extracted_fields": {"drivers": 1},
                "next_question": "Принято! Готовлю тур-превью.",
            },
            {
                "extracted_fields": {"drivers": 1},
                "next_question": "Принято! Тур ждёт.",
            },
        ]
    )
    monkeypatch = stack["monkeypatch"]
    monkeypatch.setattr(answerer, "_openrouter", stub_openrouter)
    monkeypatch.setattr(answerer, "_clock", lambda: _NOW)

    ctx1 = AnswerContext(
        chat_id=chat_id_1,
        customer_username="@danil",
        trace_id="trc-1",
        now=_NOW,
        project_id=project_id,
    )
    result1 = await answerer.try_answer(question="1 водитель", ctx=ctx1)
    assert result1.handled is True
    # send_video called once with local_path (fresh upload).
    assert len(sender.calls) == 1
    first_call = sender.calls[0]
    assert first_call["chat_id"] == chat_id_1
    assert first_call["file_id"] is None
    assert first_call["local_path"] is not None
    assert len(sender.disk_reads) == 1
    # The api should have cached the freshly assigned file_id on the row.
    refreshed = materials_repo.get(material_id=material_row.id)
    assert refreshed is not None
    assert refreshed.telegram_file_id == "FRESH-TG-VID"

    # ---- Step 3: second customer reaches the same moment → cached file_id ----
    chat_id_2 = 902
    state_repo.upsert(
        chat_id=chat_id_2,
        project_id=project_id,
        current_stage="scoping",
        collected_intent={
            "dates": "2 мая",
            "headcount": 3,
            "vehicle_count": 2,
            "difficulty": "начальный",
            "drivers": None,
        },
        now=_NOW,
        last_bot_msg_at=_NOW,
    )
    ctx2 = AnswerContext(
        chat_id=chat_id_2,
        customer_username="@second",
        trace_id="trc-2",
        now=_NOW,
        project_id=project_id,
    )
    result2 = await answerer.try_answer(question="1 водитель", ctx=ctx2)
    assert result2.handled is True
    # send_video called a second time, this time WITHOUT local_path.
    assert len(sender.calls) == 2
    second_call = sender.calls[1]
    assert second_call["chat_id"] == chat_id_2
    assert second_call["file_id"] == "FRESH-TG-VID"
    assert second_call["local_path"] is None
    # CRITICAL exit criterion: no second disk read.
    assert len(sender.disk_reads) == 1
