from __future__ import annotations

from fastapi.testclient import TestClient

from services.api.app.hitl import HitlTicketRepository
from services.web_ui.app import main as web_ui_main
from services.web_ui.app.main import app as web_ui_app


def _hitl_repo(tmp_path) -> HitlTicketRepository:
    return HitlTicketRepository(str(tmp_path / "hitl.sqlite3"))


def test_landing_page_links_to_upload(monkeypatch, tmp_path):
    monkeypatch.setattr(web_ui_main, "hitl_ticket_repository", _hitl_repo(tmp_path))
    client = TestClient(web_ui_app)

    response = client.get("/")

    assert response.status_code == 200
    assert "/knowledge-upload" in response.text


def test_upload_form_shows_operator_from_registry(monkeypatch, tmp_path):
    from services.api.app.operators import OperatorRepository
    op_repo = OperatorRepository(str(tmp_path / "operators.sqlite3"))
    op_repo.create(username="@from_registry", project_id=1)
    monkeypatch.setattr(web_ui_main, "hitl_ticket_repository", _hitl_repo(tmp_path))
    monkeypatch.setattr(web_ui_main, "operator_repository", op_repo)
    client = TestClient(web_ui_app)

    response = client.get("/knowledge-upload")

    assert response.status_code == 200
    assert "@from_registry" in response.text
    assert "enctype='multipart/form-data'" in response.text
    assert "<input type='file' name='upload'" in response.text
    assert "<textarea name='inline_text'" in response.text
    assert "name='is_confidential'" in response.text


def test_upload_form_shows_empty_when_no_operators_registered(monkeypatch, tmp_path):
    from services.api.app.operators import OperatorRepository
    empty_op_repo = OperatorRepository(str(tmp_path / "operators.sqlite3"))
    monkeypatch.setattr(web_ui_main, "hitl_ticket_repository", _hitl_repo(tmp_path))
    monkeypatch.setattr(web_ui_main, "operator_repository", empty_op_repo)
    client = TestClient(web_ui_app)

    response = client.get("/knowledge-upload")

    assert response.status_code == 200
    assert "enctype='multipart/form-data'" in response.text


def test_upload_submit_forwards_inline_text_and_renders_result(monkeypatch, tmp_path):
    monkeypatch.setattr(web_ui_main, "hitl_ticket_repository", _hitl_repo(tmp_path))
    captured: dict[str, object] = {}

    async def fake_forward(**kwargs):
        captured.update(kwargs)
        return 200, {
            "candidate_id": 42,
            "source_id": "knowledge_candidate:42",
            "inserted_chunks": 3,
            "extracted_chars": 120,
            "is_confidential": False,
            "deduplicated": False,
        }

    monkeypatch.setattr(web_ui_main, "_forward_upload_to_api", fake_forward)
    client = TestClient(web_ui_app)

    response = client.post(
        "/knowledge-upload",
        data={
            "operator_username": "@op",
            "is_confidential": "false",
            "inline_text": "Возврат товара в течение 14 дней.",
        },
    )

    assert response.status_code == 200
    assert "Upload complete" in response.text
    assert "42" in response.text
    assert "knowledge_candidate:42" in response.text
    assert captured["operator_username"] == "@op"
    assert captured["inline_text"] == "Возврат товара в течение 14 дней."
    assert captured["upload_bytes"] is None
    assert captured["is_confidential"] is False


def test_upload_submit_forwards_file(monkeypatch, tmp_path):
    monkeypatch.setattr(web_ui_main, "hitl_ticket_repository", _hitl_repo(tmp_path))
    captured: dict[str, object] = {}

    async def fake_forward(**kwargs):
        captured.update(kwargs)
        return 200, {
            "candidate_id": 7,
            "source_id": "knowledge_candidate:7",
            "inserted_chunks": 1,
            "extracted_chars": 30,
            "is_confidential": True,
            "deduplicated": True,
        }

    monkeypatch.setattr(web_ui_main, "_forward_upload_to_api", fake_forward)
    client = TestClient(web_ui_app)

    response = client.post(
        "/knowledge-upload",
        data={"operator_username": "@op", "is_confidential": "true"},
        files={"upload": ("doc.txt", b"sample bytes", "text/plain")},
    )

    assert response.status_code == 200
    assert "Upload complete" in response.text
    assert captured["upload_filename"] == "doc.txt"
    assert captured["upload_bytes"] == b"sample bytes"
    assert captured["upload_content_type"] == "text/plain"
    assert captured["inline_text"] is None
    assert captured["is_confidential"] is True


def test_upload_submit_strips_empty_inline_text_when_no_file(monkeypatch, tmp_path):
    monkeypatch.setattr(web_ui_main, "hitl_ticket_repository", _hitl_repo(tmp_path))
    captured: dict[str, object] = {}

    async def fake_forward(**kwargs):
        captured.update(kwargs)
        return 422, {"detail": "file_or_inline_text_required"}

    monkeypatch.setattr(web_ui_main, "_forward_upload_to_api", fake_forward)
    client = TestClient(web_ui_app)

    response = client.post(
        "/knowledge-upload",
        data={"operator_username": "@op", "inline_text": "   "},
    )

    assert response.status_code == 200
    assert "Upload failed" in response.text
    assert "file_or_inline_text_required" in response.text
    assert captured["inline_text"] is None
    assert captured["upload_bytes"] is None


def test_upload_submit_renders_error_on_non_2xx(monkeypatch, tmp_path):
    monkeypatch.setattr(web_ui_main, "hitl_ticket_repository", _hitl_repo(tmp_path))

    async def fake_forward(**_kwargs):
        return 500, {"detail": "operator_upload_failed"}

    monkeypatch.setattr(web_ui_main, "_forward_upload_to_api", fake_forward)
    client = TestClient(web_ui_app)

    response = client.post(
        "/knowledge-upload",
        data={"operator_username": "@op", "inline_text": "x"},
    )

    assert response.status_code == 200
    assert "Upload failed" in response.text
    assert "operator_upload_failed" in response.text
    assert "500" in response.text


def test_upload_submit_skips_empty_file_input(monkeypatch, tmp_path):
    monkeypatch.setattr(web_ui_main, "hitl_ticket_repository", _hitl_repo(tmp_path))
    captured: dict[str, object] = {}

    async def fake_forward(**kwargs):
        captured.update(kwargs)
        return 200, {
            "candidate_id": 1,
            "source_id": "knowledge_candidate:1",
            "inserted_chunks": 1,
            "extracted_chars": 5,
            "is_confidential": False,
            "deduplicated": False,
        }

    monkeypatch.setattr(web_ui_main, "_forward_upload_to_api", fake_forward)
    client = TestClient(web_ui_app)

    response = client.post(
        "/knowledge-upload",
        data={"operator_username": "@op", "inline_text": "Hello"},
    )

    assert response.status_code == 200
    assert captured["upload_filename"] is None
    assert captured["upload_bytes"] is None


def test_forward_upload_to_api_serializes_multipart(monkeypatch, tmp_path):
    monkeypatch.setattr(web_ui_main, "hitl_ticket_repository", _hitl_repo(tmp_path))
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"candidate_id": 99}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, data, files):
            captured["url"] = url
            captured["data"] = data
            captured["files"] = files
            return FakeResponse()

    import httpx as _httpx  # noqa: PLC0415

    monkeypatch.setattr(_httpx, "AsyncClient", FakeClient)

    import asyncio

    status, body = asyncio.run(
        web_ui_main._forward_upload_to_api(
            operator_username="@op",
            is_confidential=True,
            inline_text=None,
            upload_filename="doc.txt",
            upload_bytes=b"hello",
            upload_content_type="text/plain",
        )
    )

    assert status == 200
    assert body == {"candidate_id": 99}
    assert captured["url"].endswith("/knowledge/operator_upload_multipart")
    assert captured["data"]["operator_username"] == "@op"
    assert captured["data"]["is_confidential"] == "true"
    assert "inline_text" not in captured["data"]
    assert captured["files"]["upload"] == (
        "doc.txt",
        b"hello",
        "text/plain",
    )


def test_forward_upload_to_api_handles_non_json_response(monkeypatch, tmp_path):
    monkeypatch.setattr(web_ui_main, "hitl_ticket_repository", _hitl_repo(tmp_path))

    class FakeResponse:
        status_code = 502
        text = "bad gateway"

        @staticmethod
        def json() -> dict:
            raise ValueError("not json")

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *_args, **_kwargs):
            return FakeResponse()

    import httpx as _httpx  # noqa: PLC0415

    monkeypatch.setattr(_httpx, "AsyncClient", FakeClient)

    import asyncio

    status, body = asyncio.run(
        web_ui_main._forward_upload_to_api(
            operator_username="@op",
            is_confidential=False,
            inline_text="hello",
            upload_filename=None,
            upload_bytes=None,
            upload_content_type=None,
        )
    )

    assert status == 502
    assert body == {"detail": "bad gateway"}


def test_forward_upload_to_api_handles_empty_non_json_response(monkeypatch, tmp_path):
    monkeypatch.setattr(web_ui_main, "hitl_ticket_repository", _hitl_repo(tmp_path))

    class FakeResponse:
        status_code = 504
        text = ""

        @staticmethod
        def json() -> dict:
            raise ValueError("not json")

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *_args, **_kwargs):
            return FakeResponse()

    import httpx as _httpx  # noqa: PLC0415

    monkeypatch.setattr(_httpx, "AsyncClient", FakeClient)

    import asyncio

    status, body = asyncio.run(
        web_ui_main._forward_upload_to_api(
            operator_username="@op",
            is_confidential=False,
            inline_text="x",
            upload_filename=None,
            upload_bytes=None,
            upload_content_type=None,
        )
    )

    assert status == 504
    assert body == {"detail": "api_returned_non_json"}


def test_forward_upload_to_api_uses_default_filename_and_content_type(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(web_ui_main, "hitl_ticket_repository", _hitl_repo(tmp_path))
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, data, files):
            captured["files"] = files
            return FakeResponse()

    import httpx as _httpx  # noqa: PLC0415

    monkeypatch.setattr(_httpx, "AsyncClient", FakeClient)

    import asyncio

    asyncio.run(
        web_ui_main._forward_upload_to_api(
            operator_username="@op",
            is_confidential=False,
            inline_text=None,
            upload_filename=None,
            upload_bytes=b"raw",
            upload_content_type=None,
        )
    )

    assert captured["files"]["upload"] == (
        "upload.bin",
        b"raw",
        "application/octet-stream",
    )
