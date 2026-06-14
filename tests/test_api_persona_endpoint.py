from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.api.app.main import (
    app as api_app,
)
from services.api.app.main import (
    hitl_ticket_repository,
    settings,
    telegram_bot_sender,
)


@pytest.fixture(autouse=True)
def _isolated_runtime_config(tmp_path):
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    yield


def _patch_identity_calls(monkeypatch, *, ok: bool = True, error: Exception | None = None):
    """Return a dict capturing every setMyX call the endpoint makes."""
    captured: dict[str, list[dict]] = {
        "set_my_name": [],
        "set_my_description": [],
        "set_my_short_description": [],
    }

    async def _make(method_name):
        async def _call(**kwargs):
            if error is not None:
                raise error
            captured[method_name].append(kwargs)
            return {"ok": ok}

        return _call

    async def _set_my_name(**kwargs):
        if error is not None:
            raise error
        captured["set_my_name"].append(kwargs)
        return {"ok": ok}

    async def _set_my_description(**kwargs):
        if error is not None:
            raise error
        captured["set_my_description"].append(kwargs)
        return {"ok": ok}

    async def _set_my_short_description(**kwargs):
        if error is not None:
            raise error
        captured["set_my_short_description"].append(kwargs)
        return {"ok": ok}

    monkeypatch.setattr(telegram_bot_sender, "set_my_name", _set_my_name)
    monkeypatch.setattr(telegram_bot_sender, "set_my_description", _set_my_description)
    monkeypatch.setattr(
        telegram_bot_sender, "set_my_short_description", _set_my_short_description
    )
    return captured


def test_persona_endpoint_writes_config_and_calls_telegram(monkeypatch):
    captured = _patch_identity_calls(monkeypatch)
    client = TestClient(api_app)
    response = client.post(
        "/hitl/runtime-config/persona",
        json={
            "first_name": "Мария",
            "last_name": "Петрова",
            "description": "Здравствуйте, на связи.",
            "short_description": "В чате.",
            "updated_by": settings.hitl_config_admin_username,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["first_name"] == "Мария"
    assert body["last_name"] == "Петрова"
    assert body["full_name"] == "Мария Петрова"
    assert body["telegram"]["set_my_name"] == {"ok": True}

    assert (
        hitl_ticket_repository.get_runtime_config("bot_persona_first_name") == "Мария"
    )
    assert (
        hitl_ticket_repository.get_runtime_config("bot_persona_last_name")
        == "Петрова"
    )
    assert (
        hitl_ticket_repository.get_runtime_config("bot_telegram_description")
        == "Здравствуйте, на связи."
    )
    assert (
        hitl_ticket_repository.get_runtime_config("bot_telegram_short_description")
        == "В чате."
    )
    assert captured["set_my_name"] == [{"name": "Мария Петрова"}]
    assert captured["set_my_description"] == [{"description": "Здравствуйте, на связи."}]
    assert captured["set_my_short_description"] == [{"short_description": "В чате."}]


def test_persona_endpoint_uses_existing_config_when_description_omitted(monkeypatch):
    captured = _patch_identity_calls(monkeypatch)
    hitl_ticket_repository.set_runtime_config(
        key="bot_telegram_description",
        value="Стой, я живой.",
        updated_by="@ajdevy",
    )
    hitl_ticket_repository.set_runtime_config(
        key="bot_telegram_short_description",
        value="Короче.",
        updated_by="@ajdevy",
    )
    client = TestClient(api_app)
    response = client.post(
        "/hitl/runtime-config/persona",
        json={
            "first_name": "Иван",
            "last_name": "Сидоров",
            "updated_by": settings.hitl_config_admin_username,
        },
    )
    assert response.status_code == 200
    # The stored description is reused for the setMy* calls.
    assert captured["set_my_description"] == [{"description": "Стой, я живой."}]
    assert captured["set_my_short_description"] == [{"short_description": "Короче."}]


def test_persona_endpoint_falls_back_to_settings_description_when_runtime_empty(
    monkeypatch,
):
    captured = _patch_identity_calls(monkeypatch)
    client = TestClient(api_app)
    response = client.post(
        "/hitl/runtime-config/persona",
        json={
            "first_name": "Иван",
            "last_name": "Сидоров",
            "updated_by": settings.hitl_config_admin_username,
        },
    )
    assert response.status_code == 200
    assert captured["set_my_description"] == [
        {"description": settings.bot_telegram_description}
    ]
    assert captured["set_my_short_description"] == [
        {"short_description": settings.bot_telegram_short_description}
    ]


def test_persona_endpoint_rejects_invalid_name(monkeypatch):
    _patch_identity_calls(monkeypatch)
    client = TestClient(api_app)
    response = client.post(
        "/hitl/runtime-config/persona",
        json={
            "first_name": "12345",
            "last_name": "Иванова",
            "updated_by": settings.hitl_config_admin_username,
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "invalid_persona_name"


def test_persona_endpoint_rejects_too_long_name(monkeypatch):
    _patch_identity_calls(monkeypatch)
    client = TestClient(api_app)
    response = client.post(
        "/hitl/runtime-config/persona",
        json={
            "first_name": "А" * 40,
            "last_name": "Иванова",
            "updated_by": settings.hitl_config_admin_username,
        },
    )
    assert response.status_code == 422


def test_persona_endpoint_rejects_unauthorized_caller(monkeypatch):
    _patch_identity_calls(monkeypatch)
    client = TestClient(api_app)
    response = client.post(
        "/hitl/runtime-config/persona",
        json={
            "first_name": "Анна",
            "last_name": "Иванова",
            "updated_by": "@someone_else",
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "not_authorized"


def test_persona_endpoint_accepts_effective_operator(monkeypatch):
    """Endpoint accepts any caller that matches the current effective operator."""
    import services.api.app.main as api_main
    _patch_identity_calls(monkeypatch)
    monkeypatch.setattr(api_main, "_effective_hitl_operator_username", lambda: "@support_b")
    client = TestClient(api_app)
    response = client.post(
        "/hitl/runtime-config/persona",
        json={
            "first_name": "Анна",
            "last_name": "Иванова",
            "updated_by": "@support_b",
        },
    )
    assert response.status_code == 200


def test_persona_endpoint_rejects_caller_not_admin_or_effective_operator(monkeypatch):
    """Caller that is neither admin nor the effective operator is rejected."""
    import services.api.app.main as api_main
    _patch_identity_calls(monkeypatch)
    monkeypatch.setattr(api_main, "_effective_hitl_operator_username", lambda: "@support_b")
    client = TestClient(api_app)
    response = client.post(
        "/hitl/runtime-config/persona",
        json={
            "first_name": "Анна",
            "last_name": "Иванова",
            "updated_by": "@someone_random",
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "not_authorized"


def test_persona_endpoint_validates_empty_description(monkeypatch):
    _patch_identity_calls(monkeypatch)
    client = TestClient(api_app)
    response = client.post(
        "/hitl/runtime-config/persona",
        json={
            "first_name": "Анна",
            "last_name": "Иванова",
            "description": "   ",
            "updated_by": settings.hitl_config_admin_username,
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "invalid_description"


def test_persona_endpoint_validates_too_long_description(monkeypatch):
    _patch_identity_calls(monkeypatch)
    client = TestClient(api_app)
    response = client.post(
        "/hitl/runtime-config/persona",
        json={
            "first_name": "Анна",
            "last_name": "Иванова",
            "description": "Д" * 600,
            "updated_by": settings.hitl_config_admin_username,
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "invalid_description"


def test_persona_endpoint_validates_short_description(monkeypatch):
    _patch_identity_calls(monkeypatch)
    client = TestClient(api_app)
    response = client.post(
        "/hitl/runtime-config/persona",
        json={
            "first_name": "Анна",
            "last_name": "Иванова",
            "short_description": "К" * 200,
            "updated_by": settings.hitl_config_admin_username,
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "invalid_short_description"


def test_persona_endpoint_validates_empty_short_description(monkeypatch):
    _patch_identity_calls(monkeypatch)
    client = TestClient(api_app)
    response = client.post(
        "/hitl/runtime-config/persona",
        json={
            "first_name": "Анна",
            "last_name": "Иванова",
            "short_description": "   ",
            "updated_by": settings.hitl_config_admin_username,
        },
    )
    assert response.status_code == 422


def test_persona_endpoint_surfaces_telegram_error_without_raising(monkeypatch):
    """Telegram outage must not fail the endpoint — operator sees the error
    in the response body so they can retry."""

    async def _fail(**kwargs):
        raise RuntimeError("telegram_outage")

    monkeypatch.setattr(telegram_bot_sender, "set_my_name", _fail)
    monkeypatch.setattr(telegram_bot_sender, "set_my_description", _fail)
    monkeypatch.setattr(telegram_bot_sender, "set_my_short_description", _fail)
    client = TestClient(api_app)
    response = client.post(
        "/hitl/runtime-config/persona",
        json={
            "first_name": "Анна",
            "last_name": "Иванова",
            "updated_by": settings.hitl_config_admin_username,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["telegram"]["set_my_name"]["ok"] is False
    assert "telegram_outage" in body["telegram"]["set_my_name"]["error"]


def test_persona_endpoint_persists_persona_for_subsequent_reads(monkeypatch):
    _patch_identity_calls(monkeypatch)
    client = TestClient(api_app)
    client.post(
        "/hitl/runtime-config/persona",
        json={
            "first_name": "Ольга",
            "last_name": "Сергеева",
            "updated_by": settings.hitl_config_admin_username,
        },
    )
    persona = hitl_ticket_repository.get_bot_persona(
        default_first_name="X", default_last_name="Y"
    )
    assert persona == ("Ольга", "Сергеева")


def test_persona_endpoint_accepts_hyphenated_and_apostrophe_names(monkeypatch):
    _patch_identity_calls(monkeypatch)
    client = TestClient(api_app)
    response = client.post(
        "/hitl/runtime-config/persona",
        json={
            "first_name": "Анна-Мария",
            "last_name": "O'Брайен",
            "updated_by": settings.hitl_config_admin_username,
        },
    )
    assert response.status_code == 200


# Pre-baked async helper so pytest collects without warnings.
async def _noop(*args, **kwargs):
    return {"ok": True}


def test_persona_endpoint_uses_async_send_methods(monkeypatch):
    """Sanity: the endpoint awaits all three identity coroutines."""
    name_mock = AsyncMock(return_value={"ok": True})
    desc_mock = AsyncMock(return_value={"ok": True})
    short_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(telegram_bot_sender, "set_my_name", name_mock)
    monkeypatch.setattr(telegram_bot_sender, "set_my_description", desc_mock)
    monkeypatch.setattr(telegram_bot_sender, "set_my_short_description", short_mock)
    client = TestClient(api_app)
    client.post(
        "/hitl/runtime-config/persona",
        json={
            "first_name": "Анна",
            "last_name": "Иванова",
            "updated_by": settings.hitl_config_admin_username,
        },
    )
    name_mock.assert_awaited_once()
    desc_mock.assert_awaited_once()
    short_mock.assert_awaited_once()


def test_persona_endpoint_accepts_omitted_last_name(monkeypatch):
    """Last name is optional — the operator can rename the bot to a single
    given name (e.g. just «Анна»). Stored as empty string; full_name and the
    Telegram setMyName call must not have a trailing space."""
    captured = _patch_identity_calls(monkeypatch)
    client = TestClient(api_app)
    response = client.post(
        "/hitl/runtime-config/persona",
        json={
            "first_name": "Анна",
            "updated_by": settings.hitl_config_admin_username,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["first_name"] == "Анна"
    assert body["last_name"] == ""
    assert body["full_name"] == "Анна"
    assert (
        hitl_ticket_repository.get_runtime_config("bot_persona_first_name") == "Анна"
    )
    assert (
        hitl_ticket_repository.get_runtime_config("bot_persona_last_name") == ""
    )
    assert captured["set_my_name"] == [{"name": "Анна"}]


def test_persona_endpoint_accepts_empty_last_name(monkeypatch):
    """Same as omitted: explicit empty string is also accepted (clearing a
    previously-set surname)."""
    captured = _patch_identity_calls(monkeypatch)
    client = TestClient(api_app)
    response = client.post(
        "/hitl/runtime-config/persona",
        json={
            "first_name": "Анна",
            "last_name": "   ",
            "updated_by": settings.hitl_config_admin_username,
        },
    )
    assert response.status_code == 200
    assert response.json()["full_name"] == "Анна"
    assert captured["set_my_name"] == [{"name": "Анна"}]
