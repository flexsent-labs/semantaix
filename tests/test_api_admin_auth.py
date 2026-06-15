from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.main import app as api_app
from services.api.app.web_auth import WebAuthRepository
from services.bot_gateway.app.operator_files import OperatorFileRepository
from services.bot_gateway.app.telegram_update import TelegramAttachment


def _attach() -> TelegramAttachment:
    return TelegramAttachment(
        file_id="f1",
        kind="document",
        mime_type="application/pdf",
        file_size=10,
        file_name="x.pdf",
    )


@pytest.fixture
def auth_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
    operator_files_db = tmp_path / "op_files.db"
    files_repo = OperatorFileRepository(db_path=str(operator_files_db))
    # Seed an operator chat_id for @alice (operator role) and @ajdevy (admin).
    files_repo.record_upload(
        chat_id=4242,
        username="@alice",
        source_message_id=1,
        attachment=_attach(),
        is_confidential=False,
        stored_binary_path=None,
        download_status="ok",
        source_file_type="pdf",
    )
    files_repo.record_upload(
        chat_id=1111,
        username="@ajdevy",
        source_message_id=1,
        attachment=_attach(),
        is_confidential=False,
        stored_binary_path=None,
        download_status="ok",
        source_file_type="pdf",
    )

    web_auth_db = tmp_path / "web_auth.db"
    web_auth_repo = WebAuthRepository(db_path=str(web_auth_db))

    sent_dms: list[tuple[int, str]] = []

    async def fake_send(*, chat_id: int, text: str, **_: Any) -> dict[str, Any]:
        sent_dms.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(
        api_main.telegram_bot_sender, "send_message", AsyncMock(side_effect=fake_send)
    )

    monkeypatch.setattr(api_main.settings, "operator_files_db_path", str(operator_files_db))
    monkeypatch.setattr(api_main.settings, "web_auth_db_path", str(web_auth_db))
    monkeypatch.setattr(
        api_main.settings, "hitl_config_admin_username", "@ajdevy"
    )
    monkeypatch.setattr(api_main.settings, "web_session_cookie_secure", False)
    monkeypatch.setattr(api_main, "web_auth_repository", web_auth_repo)
    monkeypatch.setattr(
        api_main.admin_auth_service, "web_auth_repository", web_auth_repo
    )
    monkeypatch.setattr(api_main.admin_auth_service, "settings", api_main.settings)
    monkeypatch.setattr(
        api_main.admin_auth_service,
        "telegram_bot_sender",
        api_main.telegram_bot_sender,
    )

    client = TestClient(api_app)
    yield {
        "client": client,
        "dms": sent_dms,
        "web_auth_repo": web_auth_repo,
        "files_repo": files_repo,
    }


def test_request_code_dms_user_with_six_digit_code(auth_env: dict[str, Any]) -> None:
    client: TestClient = auth_env["client"]
    response = client.post("/admin/auth/request_code", json={"username": "@alice"})
    assert response.status_code == 200
    assert response.json() == {"sent": True}
    dms: list[tuple[int, str]] = auth_env["dms"]
    assert len(dms) == 1
    chat_id, text = dms[0]
    assert chat_id == 4242
    assert "Код входа" in text
    # Extract the 6-digit code.
    import re

    match = re.search(r"\b(\d{6})\b", text)
    assert match is not None


def test_request_code_normalizes_missing_at_prefix(auth_env: dict[str, Any]) -> None:
    client: TestClient = auth_env["client"]
    response = client.post("/admin/auth/request_code", json={"username": "alice"})
    assert response.status_code == 200
    dms: list[tuple[int, str]] = auth_env["dms"]
    assert len(dms) == 1


def test_request_code_404_for_unknown_user(auth_env: dict[str, Any]) -> None:
    client: TestClient = auth_env["client"]
    response = client.post(
        "/admin/auth/request_code", json={"username": "@nobody"}
    )
    assert response.status_code == 404
    assert auth_env["dms"] == []


def _get_code_from_dms(dms: list[tuple[int, str]]) -> str:
    import re

    text = dms[-1][1]
    match = re.search(r"\b(\d{6})\b", text)
    assert match is not None
    return match.group(1)


def test_verify_sets_session_cookie_and_returns_role_operator(
    auth_env: dict[str, Any],
) -> None:
    client: TestClient = auth_env["client"]
    client.post("/admin/auth/request_code", json={"username": "@alice"})
    code = _get_code_from_dms(auth_env["dms"])
    response = client.post(
        "/admin/auth/verify", json={"username": "@alice", "code": code}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "@alice"
    assert body["role"] == "operator"
    set_cookie = response.headers.get("set-cookie", "")
    assert "semantaix_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie.replace("Lax", "lax")


def test_verify_marks_admin_role_for_configured_admin(auth_env: dict[str, Any]) -> None:
    client: TestClient = auth_env["client"]
    client.post("/admin/auth/request_code", json={"username": "@ajdevy"})
    code = _get_code_from_dms(auth_env["dms"])
    response = client.post(
        "/admin/auth/verify", json={"username": "@ajdevy", "code": code}
    )
    assert response.status_code == 200
    assert response.json()["role"] == "admin"


def test_verify_invalid_code_returns_401(auth_env: dict[str, Any]) -> None:
    client: TestClient = auth_env["client"]
    client.post("/admin/auth/request_code", json={"username": "@alice"})
    response = client.post(
        "/admin/auth/verify", json={"username": "@alice", "code": "000000"}
    )
    assert response.status_code == 401


def test_verify_expired_code_returns_410(
    auth_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client: TestClient = auth_env["client"]
    client.post("/admin/auth/request_code", json={"username": "@alice"})
    code = _get_code_from_dms(auth_env["dms"])
    # Force expiry.
    import sqlite3
    from datetime import UTC, datetime, timedelta

    past = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    repo: WebAuthRepository = auth_env["web_auth_repo"]
    with sqlite3.connect(repo.db_path) as connection:
        connection.execute(
            "UPDATE web_auth_codes SET expires_at = ? WHERE username = ?",
            (past, "@alice"),
        )
    response = client.post(
        "/admin/auth/verify", json={"username": "@alice", "code": code}
    )
    assert response.status_code == 410


def test_verify_too_many_attempts_returns_429(auth_env: dict[str, Any]) -> None:
    client: TestClient = auth_env["client"]
    client.post("/admin/auth/request_code", json={"username": "@alice"})
    for _ in range(4):
        bad = client.post(
            "/admin/auth/verify", json={"username": "@alice", "code": "999999"}
        )
        assert bad.status_code == 401
    final = client.post(
        "/admin/auth/verify", json={"username": "@alice", "code": "999999"}
    )
    assert final.status_code == 429


def test_verify_unknown_role_returns_403(auth_env: dict[str, Any]) -> None:
    # Create a code directly for someone with neither admin nor operator status.
    repo: WebAuthRepository = auth_env["web_auth_repo"]
    code = repo.create_code(username="@stranger", chat_id=0)
    client: TestClient = auth_env["client"]
    response = client.post(
        "/admin/auth/verify", json={"username": "@stranger", "code": code}
    )
    assert response.status_code == 403


def test_me_requires_cookie(auth_env: dict[str, Any]) -> None:
    client: TestClient = auth_env["client"]
    response = client.get("/admin/auth/me")
    assert response.status_code == 401


def test_me_returns_principal_with_valid_cookie(auth_env: dict[str, Any]) -> None:
    client: TestClient = auth_env["client"]
    client.post("/admin/auth/request_code", json={"username": "@alice"})
    code = _get_code_from_dms(auth_env["dms"])
    client.post("/admin/auth/verify", json={"username": "@alice", "code": code})
    response = client.get("/admin/auth/me")
    assert response.status_code == 200
    assert response.json() == {"username": "@alice", "role": "operator"}


def test_me_with_invalid_cookie_returns_401(auth_env: dict[str, Any]) -> None:
    client: TestClient = auth_env["client"]
    client.cookies.set("semantaix_session", "deadbeef")
    response = client.get("/admin/auth/me")
    assert response.status_code == 401


def test_logout_clears_cookie_and_revokes_session(auth_env: dict[str, Any]) -> None:
    client: TestClient = auth_env["client"]
    client.post("/admin/auth/request_code", json={"username": "@alice"})
    code = _get_code_from_dms(auth_env["dms"])
    client.post("/admin/auth/verify", json={"username": "@alice", "code": code})
    response = client.post("/admin/auth/logout")
    assert response.status_code == 200
    # Cookie cleared.
    me_after = client.get("/admin/auth/me")
    assert me_after.status_code == 401


def test_logout_without_cookie_still_returns_200(auth_env: dict[str, Any]) -> None:
    client: TestClient = auth_env["client"]
    response = client.post("/admin/auth/logout")
    assert response.status_code == 200


def test_request_code_with_empty_username_returns_404(auth_env: dict[str, Any]) -> None:
    client: TestClient = auth_env["client"]
    response = client.post("/admin/auth/request_code", json={"username": "   "})
    assert response.status_code == 404


def test_role_assigned_operator_for_user_with_files_only(
    tmp_path: Path,
    auth_env: dict[str, Any],
) -> None:
    # @charlie is neither the admin nor the primary operator, but uploaded a
    # file, so they should still resolve to "operator" role.
    files_repo: OperatorFileRepository = auth_env["files_repo"]
    files_repo.record_upload(
        chat_id=7777,
        username="@charlie",
        source_message_id=1,
        attachment=_attach(),
        is_confidential=False,
        stored_binary_path=None,
        download_status="ok",
        source_file_type="pdf",
    )
    client: TestClient = auth_env["client"]
    client.post("/admin/auth/request_code", json={"username": "@charlie"})
    code = _get_code_from_dms(auth_env["dms"])
    response = client.post(
        "/admin/auth/verify", json={"username": "@charlie", "code": code}
    )
    assert response.status_code == 200
    assert response.json()["role"] == "operator"


def test_verify_rotates_prior_sessions_for_same_username(auth_env: dict[str, Any]) -> None:
    client: TestClient = auth_env["client"]
    client.post("/admin/auth/request_code", json={"username": "@alice"})
    code1 = _get_code_from_dms(auth_env["dms"])
    client.post("/admin/auth/verify", json={"username": "@alice", "code": code1})
    first_cookie = client.cookies.get("semantaix_session")
    # Second login.
    client.post("/admin/auth/request_code", json={"username": "@alice"})
    code2 = _get_code_from_dms(auth_env["dms"])
    client.post("/admin/auth/verify", json={"username": "@alice", "code": code2})
    second_cookie = client.cookies.get("semantaix_session")
    assert first_cookie != second_cookie
    # First cookie no longer valid.
    fresh_client = TestClient(api_app)
    fresh_client.cookies.set("semantaix_session", first_cookie)
    response = fresh_client.get("/admin/auth/me")
    assert response.status_code == 401
