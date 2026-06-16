from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from services.api.app.main import (
    app as api_app,
)
from services.api.app.main import (
    hitl_ticket_repository,
    settings,
    telegram_bot_sender,
)


def test_startup_skips_identity_sync_when_token_placeholder(monkeypatch, tmp_path):
    """No token → never call Telegram, even if config has overrides."""
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    monkeypatch.setattr(telegram_bot_sender, "bot_token", "replace-me")
    name_mock = AsyncMock()
    desc_mock = AsyncMock()
    short_mock = AsyncMock()
    monkeypatch.setattr(telegram_bot_sender, "set_my_name", name_mock)
    monkeypatch.setattr(telegram_bot_sender, "set_my_description", desc_mock)
    monkeypatch.setattr(telegram_bot_sender, "set_my_short_description", short_mock)

    with TestClient(api_app):
        pass

    name_mock.assert_not_awaited()
    desc_mock.assert_not_awaited()
    short_mock.assert_not_awaited()


def test_startup_pushes_persona_from_runtime_config(monkeypatch, tmp_path):
    """Configured token → push persona+descriptions from runtime config."""
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    hitl_ticket_repository.set_runtime_config(
        key="bot_persona_first_name", value="Иван", updated_by="@ajdevy"
    )
    hitl_ticket_repository.set_runtime_config(
        key="bot_persona_last_name", value="Сидоров", updated_by="@ajdevy"
    )
    hitl_ticket_repository.set_runtime_config(
        key="bot_telegram_description",
        value="Я живой человек.",
        updated_by="@ajdevy",
    )
    hitl_ticket_repository.set_runtime_config(
        key="bot_telegram_short_description",
        value="Готов помочь.",
        updated_by="@ajdevy",
    )
    monkeypatch.setattr(telegram_bot_sender, "bot_token", "real-token")
    name_mock = AsyncMock(return_value={"ok": True})
    desc_mock = AsyncMock(return_value={"ok": True})
    short_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(telegram_bot_sender, "set_my_name", name_mock)
    monkeypatch.setattr(telegram_bot_sender, "set_my_description", desc_mock)
    monkeypatch.setattr(telegram_bot_sender, "set_my_short_description", short_mock)

    with TestClient(api_app):
        pass

    name_mock.assert_awaited_once_with(name="Иван Сидоров")
    desc_mock.assert_awaited_once_with(description="Я живой человек.")
    short_mock.assert_awaited_once_with(short_description="Готов помочь.")


def test_startup_falls_back_to_settings_defaults(monkeypatch, tmp_path):
    """No runtime overrides → settings defaults are pushed."""
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    monkeypatch.setattr(telegram_bot_sender, "bot_token", "real-token")
    name_mock = AsyncMock(return_value={"ok": True})
    desc_mock = AsyncMock(return_value={"ok": True})
    short_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(telegram_bot_sender, "set_my_name", name_mock)
    monkeypatch.setattr(telegram_bot_sender, "set_my_description", desc_mock)
    monkeypatch.setattr(telegram_bot_sender, "set_my_short_description", short_mock)

    with TestClient(api_app):
        pass

    expected_name = settings.bot_persona_first_name
    name_mock.assert_awaited_once_with(name=expected_name)
    desc_mock.assert_awaited_once_with(description=settings.bot_telegram_description)
    short_mock.assert_awaited_once_with(
        short_description=settings.bot_telegram_short_description
    )


def test_startup_telegram_failure_does_not_propagate(monkeypatch, tmp_path):
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    monkeypatch.setattr(telegram_bot_sender, "bot_token", "real-token")

    async def _boom(**kwargs):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(telegram_bot_sender, "set_my_name", _boom)
    monkeypatch.setattr(telegram_bot_sender, "set_my_description", _boom)
    monkeypatch.setattr(telegram_bot_sender, "set_my_short_description", _boom)

    # Must not raise during startup.
    with TestClient(api_app):
        pass
