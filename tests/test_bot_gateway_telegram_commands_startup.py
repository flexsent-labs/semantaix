from __future__ import annotations

import pytest

from services.bot_gateway.app import main as bot_main
from services.bot_gateway.app.operator_onboarding_messages import telegram_command_menu


@pytest.mark.asyncio
async def test_startup_syncs_telegram_command_menu(monkeypatch):
    captured: list[list[dict[str, str]]] = []

    async def fake_set_my_commands(*, commands, scope=None):
        captured.append(commands)
        return {"ok": True}

    monkeypatch.setattr(
        bot_main.telegram_bot_sender,
        "set_my_commands",
        fake_set_my_commands,
    )

    await bot_main._sync_telegram_bot_commands_on_startup()

    assert captured == [telegram_command_menu()]


@pytest.mark.asyncio
async def test_startup_logs_when_set_my_commands_fails(monkeypatch, caplog):
    async def fake_set_my_commands(*, commands, scope=None):
        return {"ok": False, "description": "bad token"}

    monkeypatch.setattr(
        bot_main.telegram_bot_sender,
        "set_my_commands",
        fake_set_my_commands,
    )

    with caplog.at_level("WARNING"):
        await bot_main._sync_telegram_bot_commands_on_startup()

    assert any(
        record.message == "telegram_set_my_commands_failed" for record in caplog.records
    )
