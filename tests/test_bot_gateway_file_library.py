from __future__ import annotations

import itertools

import pytest
from fastapi.testclient import TestClient

from services.bot_gateway.app import main as bot_main
from services.bot_gateway.app.main import app as bot_app
from services.bot_gateway.app.media_group_buffer import MediaGroupBuffer
from services.bot_gateway.app.operator_files import OperatorFileRepository
from services.bot_gateway.app.telegram_file_send import TelegramFileSendError
from services.bot_gateway.app.telegram_update import TelegramAttachment


@pytest.fixture
def isolated_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(
        bot_main.settings, "persistence_db_path", str(tmp_path / "story.db")
    )
    monkeypatch.setattr(
        bot_main.settings, "hitl_ticket_db_path", str(tmp_path / "hitl.db")
    )
    monkeypatch.setattr(bot_main.settings, "telegram_bot_token", "TKN")
    monkeypatch.setattr(
        bot_main.settings, "hitl_primary_operator_username", "@ajdevy"
    )

    class _StubHitlRepo:
        def get_runtime_config(self, key):
            return None

        def set_runtime_config(self, **kwargs):
            pass

        def list_all(self):
            return []

    monkeypatch.setattr(bot_main, "hitl_ticket_repository", _StubHitlRepo())
    fresh_files_repo = OperatorFileRepository(
        str(tmp_path / "operator_files.db")
    )
    monkeypatch.setattr(bot_main, "operator_file_repository", fresh_files_repo)
    fresh_buffer = MediaGroupBuffer(str(tmp_path / "hitl.db"))
    monkeypatch.setattr(bot_main, "media_group_buffer", fresh_buffer)

    sent_dms: list[tuple[int, str]] = []

    async def fake_send_dm(chat_id, text):
        sent_dms.append((chat_id, text))

    monkeypatch.setattr(bot_main, "_send_dm", fake_send_dm)
    return {"tmp_path": tmp_path, "dms": sent_dms, "files_repo": fresh_files_repo}


def _seed(repo, *, n=1, username="@ajdevy", base_id="ALPHA"):
    records = []
    for i in range(n):
        records.append(
            repo.record_upload(
                chat_id=100,
                username=username,
                source_message_id=i,
                attachment=TelegramAttachment(
                    file_id=f"{base_id}{i}",
                    kind="document",
                    mime_type="application/pdf",
                    file_size=1024 * (i + 1),
                    file_name=f"file_{i}.pdf",
                ),
                is_confidential=(i == 0),
                stored_binary_path=None,
                download_status="ok",
                source_file_type="pdf",
                kb_ingest_status="ok",
                kb_inserted_chunks=3,
            )
        )
    return records


_WEBHOOK_UPDATE_IDS = itertools.count(1)


def _msg(text: str, username: str = "ajdevy") -> dict:
    # Unique update_id per delivery: Story 12.31's entry-claim dedup drops a
    # repeated update_id, so two distinct operator commands in one test (e.g.
    # delete + confirm) must not share one.
    update_id = next(_WEBHOOK_UPDATE_IDS)
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "chat": {"id": 100},
            "from": {"id": 200, "username": username},
            "text": text,
        },
    }


def test_files_command_returns_recent_uploads(isolated_bot):
    _seed(isolated_bot["files_repo"], n=3)
    client = TestClient(bot_app)
    resp = client.post("/telegram/webhook", json=_msg("/files"))
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["route"] == "files_list"
    dms = [t for _, t in isolated_bot["dms"]]
    assert dms
    listing = dms[-1]
    # Newest first
    assert listing.index("file_2.pdf") < listing.index("file_0.pdf")
    # Confidential icon on the seeded confidential row (i==0)
    assert "🔒" in listing


def test_files_empty_returns_empty_message(isolated_bot):
    client = TestClient(bot_app)
    resp = client.post("/telegram/webhook", json=_msg("/files"))
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["route"] == "files_list"
    dms = [t for _, t in isolated_bot["dms"]]
    assert any("Пока нет сохранённых файлов" in t for t in dms)


def test_files_command_respects_explicit_limit(isolated_bot):
    _seed(isolated_bot["files_repo"], n=4)
    client = TestClient(bot_app)
    client.post("/telegram/webhook", json=_msg("/files 2"))
    dms = [t for _, t in isolated_bot["dms"]]
    listing = dms[-1]
    # Only the 2 newest names appear
    assert "file_3.pdf" in listing
    assert "file_2.pdf" in listing
    assert "file_1.pdf" not in listing
    assert "file_0.pdf" not in listing


def test_files_command_clamps_to_max_limit(isolated_bot, monkeypatch):
    monkeypatch.setattr(bot_main.settings, "operator_files_list_max_limit", 3)
    _seed(isolated_bot["files_repo"], n=10)
    client = TestClient(bot_app)
    client.post("/telegram/webhook", json=_msg("/files 999"))
    dms = [t for _, t in isolated_bot["dms"]]
    listing = dms[-1]
    # Only 3 names should appear (clamped)
    appearing = sum(
        1 for i in range(10) if f"file_{i}.pdf" in listing
    )
    assert appearing == 3


def test_files_command_from_non_operator_ignored(isolated_bot):
    _seed(isolated_bot["files_repo"], n=1)
    client = TestClient(bot_app)
    resp = client.post(
        "/telegram/webhook", json=_msg("/files", username="stranger")
    )
    body = resp.json()
    # Falls through; the normal customer-forward path runs.
    assert body.get("route") != "files_list"


def test_files_invalid_limit_argument_uses_default(isolated_bot, monkeypatch):
    _seed(isolated_bot["files_repo"], n=2)
    client = TestClient(bot_app)
    client.post("/telegram/webhook", json=_msg("/files abc"))
    dms = [t for _, t in isolated_bot["dms"]]
    listing = dms[-1]
    assert "file_0.pdf" in listing
    assert "file_1.pdf" in listing


def test_send_command_by_file_id_happy_path(isolated_bot, monkeypatch):
    records = _seed(isolated_bot["files_repo"], n=1)
    record = records[0]

    captured: dict = {}

    async def fake_send_by_id(*, chat_id, file_id, caption=None):
        captured["chat_id"] = chat_id
        captured["file_id"] = file_id
        return {"ok": True, "result": {"message_id": 42}}

    async def fake_send_local(**kwargs):  # pragma: no cover - must not run
        raise AssertionError("local path must not be taken on happy path")

    monkeypatch.setattr(
        bot_main.telegram_file_sender,
        "send_document_by_file_id",
        fake_send_by_id,
    )
    monkeypatch.setattr(
        bot_main.telegram_file_sender,
        "send_document_local",
        fake_send_local,
    )

    client = TestClient(bot_app)
    resp = client.post(
        "/telegram/webhook", json=_msg(f"/send {record.short_id} @bob")
    )
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["route"] == "file_send"
    assert captured["chat_id"] == "@bob"
    assert captured["file_id"] == record.telegram_file_id
    dms = [t for _, t in isolated_bot["dms"]]
    assert any("Файл отправлен" in t for t in dms)


def test_send_command_accepts_numeric_chat_id(isolated_bot, monkeypatch):
    record = _seed(isolated_bot["files_repo"], n=1)[0]
    captured: dict = {}

    async def fake_send_by_id(*, chat_id, file_id, caption=None):
        captured["chat_id"] = chat_id
        return {"ok": True}

    monkeypatch.setattr(
        bot_main.telegram_file_sender,
        "send_document_by_file_id",
        fake_send_by_id,
    )
    client = TestClient(bot_app)
    client.post(
        "/telegram/webhook", json=_msg(f"/send {record.short_id} 12345")
    )
    assert captured["chat_id"] == 12345


def test_send_command_accepts_negative_numeric_chat_id(
    isolated_bot, monkeypatch
):
    record = _seed(isolated_bot["files_repo"], n=1)[0]
    captured: dict = {}

    async def fake_send_by_id(*, chat_id, file_id, caption=None):
        captured["chat_id"] = chat_id
        return {"ok": True}

    monkeypatch.setattr(
        bot_main.telegram_file_sender,
        "send_document_by_file_id",
        fake_send_by_id,
    )
    client = TestClient(bot_app)
    client.post(
        "/telegram/webhook",
        json=_msg(f"/send {record.short_id} -10042"),
    )
    assert captured["chat_id"] == -10042


def test_send_command_unknown_short_id_returns_error(isolated_bot, monkeypatch):
    called: list[tuple] = []

    async def fake_send_by_id(**kwargs):  # pragma: no cover
        called.append((kwargs,))
        return {"ok": True}

    monkeypatch.setattr(
        bot_main.telegram_file_sender,
        "send_document_by_file_id",
        fake_send_by_id,
    )
    client = TestClient(bot_app)
    resp = client.post(
        "/telegram/webhook", json=_msg("/send UNKNOWN1 @bob")
    )
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["route"] == "file_send"
    assert body["decision"] == "short_id_unknown"
    assert called == []
    dms = [t for _, t in isolated_bot["dms"]]
    assert any("не найден" in t for t in dms)


def test_send_command_bad_format_returns_help(isolated_bot, monkeypatch):
    record = _seed(isolated_bot["files_repo"], n=1)[0]
    called: list[tuple] = []

    async def fake_send_by_id(**kwargs):  # pragma: no cover
        called.append((kwargs,))
        return {}

    monkeypatch.setattr(
        bot_main.telegram_file_sender,
        "send_document_by_file_id",
        fake_send_by_id,
    )
    client = TestClient(bot_app)
    resp = client.post(
        "/telegram/webhook",
        json=_msg(f"/send {record.short_id}"),
    )
    body = resp.json()
    assert body["decision"] == "bad_format"
    assert called == []
    dms = [t for _, t in isolated_bot["dms"]]
    assert any("Использование" in t for t in dms)


def test_send_command_falls_back_to_local_file_on_telegram_error(
    isolated_bot, monkeypatch
):
    pdf = isolated_bot["tmp_path"] / "saved.pdf"
    pdf.write_bytes(b"%PDF-LOCAL")

    record = isolated_bot["files_repo"].record_upload(
        chat_id=100,
        username="@ajdevy",
        source_message_id=1,
        attachment=TelegramAttachment(
            file_id="STALE_ID",
            kind="document",
            file_name="saved.pdf",
            mime_type="application/pdf",
            file_size=10,
        ),
        is_confidential=False,
        stored_binary_path=str(pdf),
        download_status="ok",
        source_file_type="pdf",
        kb_ingest_status="ok",
        kb_inserted_chunks=1,
    )

    async def fake_send_by_id(**kwargs):
        raise TelegramFileSendError(
            "telegram_send_failed",
            description="Bad Request: file_id is invalid",
        )

    local_called: dict = {}

    async def fake_send_local(*, chat_id, path, file_name=None, caption=None):
        local_called["path"] = path
        local_called["file_name"] = file_name
        return {"ok": True}

    monkeypatch.setattr(
        bot_main.telegram_file_sender,
        "send_document_by_file_id",
        fake_send_by_id,
    )
    monkeypatch.setattr(
        bot_main.telegram_file_sender,
        "send_document_local",
        fake_send_local,
    )

    client = TestClient(bot_app)
    resp = client.post(
        "/telegram/webhook", json=_msg(f"/send {record.short_id} @bob")
    )
    body = resp.json()
    assert body["decision"] == "sent_local"
    assert local_called["file_name"] == "saved.pdf"


def test_send_command_no_local_fallback_when_no_path(isolated_bot, monkeypatch):
    record = _seed(isolated_bot["files_repo"], n=1)[0]

    async def fake_send_by_id(**kwargs):
        raise TelegramFileSendError(
            "telegram_send_failed", description="Forbidden: bot blocked"
        )

    monkeypatch.setattr(
        bot_main.telegram_file_sender,
        "send_document_by_file_id",
        fake_send_by_id,
    )
    client = TestClient(bot_app)
    resp = client.post(
        "/telegram/webhook", json=_msg(f"/send {record.short_id} @bob")
    )
    body = resp.json()
    assert body["decision"] == "send_failed"
    dms = [t for _, t in isolated_bot["dms"]]
    assert any("Forbidden" in t for t in dms)


def test_send_command_from_non_operator_ignored(isolated_bot, monkeypatch):
    _seed(isolated_bot["files_repo"], n=1)
    called: list[tuple] = []

    async def fake_send_by_id(**kwargs):  # pragma: no cover
        called.append((kwargs,))
        return {}

    monkeypatch.setattr(
        bot_main.telegram_file_sender,
        "send_document_by_file_id",
        fake_send_by_id,
    )
    client = TestClient(bot_app)
    resp = client.post(
        "/telegram/webhook",
        json=_msg("/send ANY1 @bob", username="stranger"),
    )
    body = resp.json()
    assert body.get("route") != "file_send"
    assert called == []


def test_send_command_bad_target_returns_help(isolated_bot, monkeypatch):
    record = _seed(isolated_bot["files_repo"], n=1)[0]
    called: list[tuple] = []

    async def fake_send_by_id(**kwargs):  # pragma: no cover
        called.append((kwargs,))
        return {}

    monkeypatch.setattr(
        bot_main.telegram_file_sender,
        "send_document_by_file_id",
        fake_send_by_id,
    )
    client = TestClient(bot_app)
    resp = client.post(
        "/telegram/webhook",
        json=_msg(f"/send {record.short_id} not-a-username"),
    )
    body = resp.json()
    assert body["decision"] == "bad_target"
    assert called == []
    dms = [t for _, t in isolated_bot["dms"]]
    assert any("Использование" in t for t in dms)


def test_send_command_local_fallback_also_fails(isolated_bot, monkeypatch):
    pdf = isolated_bot["tmp_path"] / "saved.pdf"
    pdf.write_bytes(b"%PDF-LOCAL")
    record = isolated_bot["files_repo"].record_upload(
        chat_id=100,
        username="@ajdevy",
        source_message_id=1,
        attachment=bot_main.TelegramAttachment(
            file_id="STALE",
            kind="document",
            file_name="saved.pdf",
            mime_type="application/pdf",
            file_size=10,
        ),
        is_confidential=False,
        stored_binary_path=str(pdf),
        download_status="ok",
        source_file_type="pdf",
        kb_ingest_status="ok",
        kb_inserted_chunks=1,
    )

    async def fake_send_by_id(**kwargs):
        raise TelegramFileSendError(
            "telegram_send_failed", description="file_id invalid"
        )

    async def fake_send_local(**kwargs):
        raise TelegramFileSendError(
            "telegram_send_failed", description="Bad Request: chat not found"
        )

    monkeypatch.setattr(
        bot_main.telegram_file_sender,
        "send_document_by_file_id",
        fake_send_by_id,
    )
    monkeypatch.setattr(
        bot_main.telegram_file_sender,
        "send_document_local",
        fake_send_local,
    )
    client = TestClient(bot_app)
    resp = client.post(
        "/telegram/webhook", json=_msg(f"/send {record.short_id} @bob")
    )
    body = resp.json()
    assert body["decision"] == "send_failed"
    dms = [t for _, t in isolated_bot["dms"]]
    assert any("chat not found" in t for t in dms)


def _install_delete_fakes(monkeypatch, *, fetch_returns, delete_returns,
                          delete_all_returns):
    """Wire fake ApiClient methods on the live bot_main.api_client instance.

    Each handler receives ``**kwargs`` so tests can assert on the requester.
    """
    fetch_calls: list[dict] = []
    delete_calls: list[dict] = []
    delete_all_calls: list[dict] = []

    async def fake_fetch(**kwargs):
        fetch_calls.append(kwargs)
        return fetch_returns

    async def fake_delete(**kwargs):
        delete_calls.append(kwargs)
        return delete_returns

    async def fake_delete_all(**kwargs):
        delete_all_calls.append(kwargs)
        return delete_all_returns

    monkeypatch.setattr(bot_main.api_client, "fetch_file_inspect", fake_fetch)
    monkeypatch.setattr(bot_main.api_client, "delete_operator_file", fake_delete)
    monkeypatch.setattr(
        bot_main.api_client, "delete_all_operator_files", fake_delete_all
    )
    return {
        "fetch_calls": fetch_calls,
        "delete_calls": delete_calls,
        "delete_all_calls": delete_all_calls,
    }


def test_file_delete_without_confirm_emits_warning(isolated_bot, monkeypatch):
    record = _seed(isolated_bot["files_repo"], n=1)[0]
    fakes = _install_delete_fakes(
        monkeypatch,
        fetch_returns={
            "short_id": record.short_id,
            "source_file_name": "file_0.pdf",
        },
        delete_returns=None,
        delete_all_returns=None,
    )
    client = TestClient(bot_app)
    resp = client.post(
        "/telegram/webhook", json=_msg(f"/file_delete {record.short_id}")
    )
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["route"] == "file_delete"
    assert body["decision"] == "warn"
    assert fakes["delete_calls"] == []
    dms = [t for _, t in isolated_bot["dms"]]
    assert any("file_0.pdf" in t and "Подтвердите" in t for t in dms)


def test_file_delete_unknown_short_id_emits_not_found(isolated_bot, monkeypatch):
    fakes = _install_delete_fakes(
        monkeypatch,
        fetch_returns=None,
        delete_returns=None,
        delete_all_returns=None,
    )
    client = TestClient(bot_app)
    resp = client.post(
        "/telegram/webhook", json=_msg("/file_delete NOPE1234")
    )
    body = resp.json()
    assert body["decision"] == "not_found"
    assert fakes["delete_calls"] == []
    dms = [t for _, t in isolated_bot["dms"]]
    assert any("не найден" in t for t in dms)


def test_file_delete_with_confirm_invokes_api_and_dms_summary(
    isolated_bot, monkeypatch
):
    fakes = _install_delete_fakes(
        monkeypatch,
        fetch_returns={
            "short_id": "ABC1",
            "source_file_name": "abc.pdf",
        },
        delete_returns={
            "deleted_files": 1,
            "deleted_chunks": 3,
            "deleted_candidates": 1,
            "deleted_binaries": 1,
            "failed_binary_paths": [],
        },
        delete_all_returns=None,
    )
    client = TestClient(bot_app)
    resp = client.post(
        "/telegram/webhook", json=_msg("/file_delete ABC1 confirm")
    )
    body = resp.json()
    assert body["decision"] == "deleted"
    assert fakes["delete_calls"][0]["short_id"] == "ABC1"
    assert fakes["delete_calls"][0]["requester_username"] == "@ajdevy"
    dms = [t for _, t in isolated_bot["dms"]]
    assert any("Удалено" in t and "файлов: 1" in t for t in dms)


def test_file_delete_confirm_token_case_insensitive(isolated_bot, monkeypatch):
    fakes = _install_delete_fakes(
        monkeypatch,
        fetch_returns=None,
        delete_returns={
            "deleted_files": 1,
            "deleted_chunks": 0,
            "deleted_candidates": 0,
            "deleted_binaries": 0,
            "failed_binary_paths": [],
        },
        delete_all_returns=None,
    )
    client = TestClient(bot_app)
    client.post("/telegram/webhook", json=_msg("/file_delete XYZ1 CONFIRM"))
    client.post("/telegram/webhook", json=_msg("/file_delete xyz2 Confirm"))
    assert len(fakes["delete_calls"]) == 2
    # Short ids are upper-cased before being sent.
    assert fakes["delete_calls"][0]["short_id"] == "XYZ1"
    assert fakes["delete_calls"][1]["short_id"] == "XYZ2"


def test_file_delete_confirm_returns_404_dms_not_found(isolated_bot, monkeypatch):
    fakes = _install_delete_fakes(
        monkeypatch,
        fetch_returns=None,
        delete_returns=None,
        delete_all_returns=None,
    )
    client = TestClient(bot_app)
    resp = client.post(
        "/telegram/webhook", json=_msg("/file_delete GONE1 confirm")
    )
    assert resp.json()["decision"] == "not_found"
    dms = [t for _, t in isolated_bot["dms"]]
    assert any("не найден" in t for t in dms)
    assert fakes["delete_calls"]  # call was made before the 404


def test_file_delete_missing_short_id_emits_usage(isolated_bot, monkeypatch):
    fakes = _install_delete_fakes(
        monkeypatch,
        fetch_returns=None,
        delete_returns=None,
        delete_all_returns=None,
    )
    client = TestClient(bot_app)
    resp = client.post("/telegram/webhook", json=_msg("/file_delete"))
    assert resp.json()["decision"] == "usage"
    assert fakes["fetch_calls"] == []
    dms = [t for _, t in isolated_bot["dms"]]
    assert any("Использование" in t for t in dms)


def test_file_delete_from_non_operator_non_admin_ignored(isolated_bot, monkeypatch):
    fakes = _install_delete_fakes(
        monkeypatch,
        fetch_returns=None,
        delete_returns=None,
        delete_all_returns=None,
    )
    client = TestClient(bot_app)
    resp = client.post(
        "/telegram/webhook",
        json=_msg("/file_delete ABC1 confirm", username="stranger"),
    )
    body = resp.json()
    assert body.get("route") != "file_delete"
    assert fakes["delete_calls"] == []


def test_file_delete_admin_can_run(isolated_bot, monkeypatch):
    # The fixture pins @ajdevy as primary operator AND admin (same username
    # in this fixture); set distinct admin so we exercise the admin branch.
    monkeypatch.setattr(
        bot_main.settings, "hitl_primary_operator_username", "@alice"
    )
    monkeypatch.setattr(
        bot_main.settings, "hitl_config_admin_username", "@ajdevy"
    )
    fakes = _install_delete_fakes(
        monkeypatch,
        fetch_returns=None,
        delete_returns={
            "deleted_files": 1,
            "deleted_chunks": 0,
            "deleted_candidates": 0,
            "deleted_binaries": 0,
            "failed_binary_paths": [],
        },
        delete_all_returns=None,
    )
    client = TestClient(bot_app)
    resp = client.post(
        "/telegram/webhook",
        json=_msg("/file_delete OTHR1 confirm", username="ajdevy"),
    )
    assert resp.json()["decision"] == "deleted"
    assert fakes["delete_calls"][0]["requester_username"] == "@ajdevy"


def test_file_delete_summary_lists_failed_binaries(isolated_bot, monkeypatch):
    fakes = _install_delete_fakes(
        monkeypatch,
        fetch_returns=None,
        delete_returns={
            "deleted_files": 1,
            "deleted_chunks": 0,
            "deleted_candidates": 0,
            "deleted_binaries": 0,
            "failed_binary_paths": ["/data/stuck.bin"],
        },
        delete_all_returns=None,
    )
    client = TestClient(bot_app)
    client.post("/telegram/webhook", json=_msg("/file_delete STUCK1 confirm"))
    assert fakes["delete_calls"]
    dms = [t for _, t in isolated_bot["dms"]]
    assert any("Не удалось удалить файлы с диска" in t for t in dms)


def test_files_delete_all_without_confirm_zero_files(isolated_bot, monkeypatch):
    fakes = _install_delete_fakes(
        monkeypatch,
        fetch_returns=None,
        delete_returns=None,
        delete_all_returns=None,
    )
    client = TestClient(bot_app)
    resp = client.post("/telegram/webhook", json=_msg("/files_delete_all"))
    assert resp.json()["decision"] == "empty"
    assert fakes["delete_all_calls"] == []
    dms = [t for _, t in isolated_bot["dms"]]
    assert any("нет сохранённых файлов" in t for t in dms)


def test_files_delete_all_without_confirm_with_files(isolated_bot, monkeypatch):
    _seed(isolated_bot["files_repo"], n=3)
    fakes = _install_delete_fakes(
        monkeypatch,
        fetch_returns=None,
        delete_returns=None,
        delete_all_returns=None,
    )
    client = TestClient(bot_app)
    resp = client.post("/telegram/webhook", json=_msg("/files_delete_all"))
    body = resp.json()
    assert body["decision"] == "warn"
    assert body["count"] == "3"
    assert fakes["delete_all_calls"] == []
    dms = [t for _, t in isolated_bot["dms"]]
    assert any("3 файлов" in t and "Подтвердите" in t for t in dms)


def test_files_delete_all_with_confirm_invokes_api(isolated_bot, monkeypatch):
    fakes = _install_delete_fakes(
        monkeypatch,
        fetch_returns=None,
        delete_returns=None,
        delete_all_returns={
            "deleted_files": 3,
            "deleted_chunks": 9,
            "deleted_candidates": 3,
            "deleted_binaries": 2,
            "failed_binary_paths": [],
        },
    )
    client = TestClient(bot_app)
    resp = client.post(
        "/telegram/webhook", json=_msg("/files_delete_all confirm")
    )
    body = resp.json()
    assert body["decision"] == "deleted"
    assert body["count"] == "3"
    assert fakes["delete_all_calls"][0]["requester_username"] == "@ajdevy"
    dms = [t for _, t in isolated_bot["dms"]]
    assert any("файлов: 3" in t for t in dms)


def test_files_delete_all_from_non_operator_non_admin_ignored(
    isolated_bot, monkeypatch
):
    fakes = _install_delete_fakes(
        monkeypatch,
        fetch_returns=None,
        delete_returns=None,
        delete_all_returns=None,
    )
    client = TestClient(bot_app)
    resp = client.post(
        "/telegram/webhook",
        json=_msg("/files_delete_all confirm", username="stranger"),
    )
    body = resp.json()
    assert body.get("route") != "files_delete_all"
    assert fakes["delete_all_calls"] == []


def test_files_listing_renders_skipped_and_failed_glyphs(isolated_bot):
    repo = isolated_bot["files_repo"]
    repo.record_upload(
        chat_id=100,
        username="@ajdevy",
        source_message_id=1,
        attachment=bot_main.TelegramAttachment(
            file_id="A",
            kind="document",
            file_name="too_big.pdf",
            mime_type="application/pdf",
            file_size=50_000_000,
        ),
        is_confidential=False,
        stored_binary_path=None,
        download_status="too_large",
        source_file_type="pdf",
        kb_ingest_status="skipped",
    )
    repo.record_upload(
        chat_id=100,
        username="@ajdevy",
        source_message_id=2,
        attachment=bot_main.TelegramAttachment(
            file_id="B",
            kind="document",
            file_name="bad.pdf",
            mime_type="application/pdf",
            file_size=10,
        ),
        is_confidential=False,
        stored_binary_path="/x",
        download_status="ok",
        source_file_type="pdf",
        kb_ingest_status="failed:api boom",
    )
    repo.record_upload(
        chat_id=100,
        username="@ajdevy",
        source_message_id=3,
        attachment=bot_main.TelegramAttachment(
            file_id="C",
            kind="document",
            file_name="pending.pdf",
            mime_type="application/pdf",
            file_size=5,
        ),
        is_confidential=False,
        stored_binary_path="/x",
        download_status="ok",
        source_file_type="pdf",
        kb_ingest_status="pending",
    )
    client = TestClient(bot_app)
    client.post("/telegram/webhook", json=_msg("/files"))
    listing = isolated_bot["dms"][-1][1]
    assert "⏭️" in listing  # skipped
    assert "❌" in listing  # failed
    assert "…" in listing  # pending
