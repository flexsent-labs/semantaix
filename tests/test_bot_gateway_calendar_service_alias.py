"""Unit tests for the `/calendar_service` alias + migration-hint DM
(Epic 13, story 13.03).

Covers:
- `/calendar_service add ...` still works (delegates → existing handler).
- Every call emits the `deprecation_warning_calendar_service_command` log line.
- First call per `(project, operator)` → DMs the migration hint + records to
  the dedup table.
- Second call → does NOT DM the hint again (dedup hit).
- Different operator on same project → DMs the hint (per-operator dedup).
"""

from __future__ import annotations

import itertools
import logging

import pytest
from fastapi.testclient import TestClient

from services.bot_gateway.app import main as bot_main
from services.bot_gateway.app.main import app as bot_app

_OPERATOR = "@calendar_op"
_OPERATOR_2 = "@another_op"
_PROJECT_ID = 11


class _StubHitlRepo:
    def get_runtime_config(self, key: str):
        return None

    def set_runtime_config(self, **kwargs):
        pass

    def list_all(self):
        return []


@pytest.fixture
def isolated_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(
        bot_main.settings, "persistence_db_path", str(tmp_path / "story.db")
    )
    monkeypatch.setattr(
        bot_main.settings, "hitl_ticket_db_path", str(tmp_path / "hitl.db")
    )
    monkeypatch.setattr(bot_main.settings, "telegram_bot_token", "TKN")
    monkeypatch.setattr(bot_main.settings, "internal_service_token", "svc-token")
    monkeypatch.setattr(
        bot_main.settings, "nl_ops_db_path", str(tmp_path / "nl_ops.db")
    )
    # The alias hint repo's schema is bootstrapped by api startup; the bot
    # writes to the same DB, so it must exist before the first call.
    from services.api.app.calendar.calendar_service_alias_hint_repository import (
        init_calendar_service_alias_hint_schema,
    )

    init_calendar_service_alias_hint_schema(bot_main.settings.nl_ops_db_path)
    monkeypatch.setattr(bot_main, "hitl_ticket_repository", _StubHitlRepo())

    sent_dms: list[tuple[int, str]] = []

    async def fake_send_dm(chat_id: int, text: str) -> None:
        sent_dms.append((chat_id, text))

    monkeypatch.setattr(bot_main, "_send_dm", fake_send_dm)
    return {"tmp_path": tmp_path, "dms": sent_dms}


_WEBHOOK_UPDATE_IDS = itertools.count(1)


def _message(*, text: str, username: str = "calendar_op", chat_id: int = 100):
    # Each webhook delivery is a distinct Telegram update; give it a unique
    # update_id so Story 12.31's entry-claim dedup does not collapse two
    # genuinely different operator messages posted within one test.
    update_id = next(_WEBHOOK_UPDATE_IDS)
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "chat": {"id": chat_id},
            "from": {"id": 200, "username": username},
            "text": text,
        },
    }


def _stub_operator_lookup(monkeypatch, *, username: str):
    async def fake_lookup(*, username: str):
        return {
            "username": username,
            "chat_id": 100,
            "project_id": _PROJECT_ID,
            "is_active": True,
        }

    monkeypatch.setattr(bot_main.api_client, "find_operator_by_username", fake_lookup)


def _stub_legacy_upsert(monkeypatch):
    captured: list[dict] = []

    async def fake_upsert(**kwargs):
        captured.append(kwargs)
        return {"id": 5}

    monkeypatch.setattr(bot_main.api_client, "calendar_upsert_service", fake_upsert)
    return captured


def test_alias_first_call_dms_hint_and_completes(isolated_bot, monkeypatch, caplog):
    _stub_operator_lookup(monkeypatch, username=_OPERATOR)
    upserts = _stub_legacy_upsert(monkeypatch)

    client = TestClient(bot_app)
    with caplog.at_level(logging.INFO, logger="services.bot_gateway.app.calendar_commands"):
        response = client.post(
            "/telegram/webhook",
            json=_message(
                text="/calendar_service add маникюр 60 mon-sat 10:00-19:00"
            ),
        )
    body = response.json()
    assert body["route"] == "calendar_service"
    assert body["decision"] == "added"
    # The legacy handler still ran and persisted the row.
    assert len(upserts) == 1
    # First DM is the migration hint; second is the legacy success message.
    assert len(isolated_bot["dms"]) == 2
    assert "устарела" in isolated_bot["dms"][0][1]
    assert "сохранена" in isolated_bot["dms"][1][1]
    # Deprecation log entry must be present on every call.
    log_messages = [r.message for r in caplog.records]
    assert "deprecation_warning_calendar_service_command" in log_messages


def test_alias_second_call_same_operator_skips_hint(isolated_bot, monkeypatch, caplog):
    _stub_operator_lookup(monkeypatch, username=_OPERATOR)
    _stub_legacy_upsert(monkeypatch)

    client = TestClient(bot_app)
    # First call: hint + success.
    client.post(
        "/telegram/webhook",
        json=_message(text="/calendar_service add x 60 mon 10:00-19:00"),
    )
    dms_after_first = len(isolated_bot["dms"])
    assert dms_after_first == 2

    # Second call: no hint, but deprecation log still fires.
    with caplog.at_level(logging.INFO, logger="services.bot_gateway.app.calendar_commands"):
        response = client.post(
            "/telegram/webhook",
            json=_message(text="/calendar_service add y 30 tue 09:00-18:00"),
        )
    assert response.json()["decision"] == "added"
    # Only one new DM (the success confirmation; no hint repeat).
    assert len(isolated_bot["dms"]) == dms_after_first + 1
    assert "устарела" not in isolated_bot["dms"][-1][1]
    log_messages = [r.message for r in caplog.records]
    assert log_messages.count("deprecation_warning_calendar_service_command") >= 1


def test_alias_different_operator_dms_hint(isolated_bot, monkeypatch):
    """Per-(project, operator) dedup: a different operator on the same project
    still receives the hint."""
    _stub_operator_lookup(monkeypatch, username=_OPERATOR)
    _stub_legacy_upsert(monkeypatch)

    client = TestClient(bot_app)
    # Operator 1: gets hint.
    client.post(
        "/telegram/webhook",
        json=_message(text="/calendar_service add a 60 mon 10:00-19:00"),
    )
    after_op1 = len(isolated_bot["dms"])  # hint + success = 2

    # Swap the operator lookup to a different user.
    _stub_operator_lookup(monkeypatch, username=_OPERATOR_2)

    # Operator 2: should also get the hint (dedup is per-operator).
    response = client.post(
        "/telegram/webhook",
        json=_message(
            text="/calendar_service add b 30 tue 09:00-18:00",
            username="another_op",
        ),
    )
    assert response.json()["decision"] == "added"
    new_dms = isolated_bot["dms"][after_op1:]
    assert len(new_dms) == 2  # hint + success
    assert "устарела" in new_dms[0][1]


def test_alias_dedup_failure_does_not_block_action(isolated_bot, monkeypatch, caplog):
    """If the hint-dedup repo raises, the user's action still completes."""
    _stub_operator_lookup(monkeypatch, username=_OPERATOR)
    upserts = _stub_legacy_upsert(monkeypatch)

    def boom(**kwargs):
        raise RuntimeError("dedup table is gone")

    monkeypatch.setattr(
        "services.bot_gateway.app.calendar_commands."
        "should_send_calendar_service_alias_hint",
        boom,
    )

    client = TestClient(bot_app)
    with caplog.at_level(logging.WARNING, logger="services.bot_gateway.app.calendar_commands"):
        response = client.post(
            "/telegram/webhook",
            json=_message(text="/calendar_service add x 60 mon 10:00-19:00"),
        )
    body = response.json()
    assert body["decision"] == "added"
    assert len(upserts) == 1
    # No hint DM (dedup raised → we treat as "do not DM"), but the success
    # DM is still delivered.
    assert "устарела" not in isolated_bot["dms"][0][1]
    log_messages = [r.message for r in caplog.records]
    assert "calendar_service_alias_hint_dedup_failed" in log_messages


def test_alias_skips_hint_when_db_path_not_provided(monkeypatch, tmp_path):
    """When the dispatcher is invoked WITHOUT a ``nl_ops_db_path``, the alias
    still works but no hint is sent (the dedup is silently disabled)."""
    monkeypatch.setattr(
        bot_main.settings, "persistence_db_path", str(tmp_path / "story.db")
    )
    monkeypatch.setattr(
        bot_main.settings, "hitl_ticket_db_path", str(tmp_path / "hitl.db")
    )
    monkeypatch.setattr(bot_main.settings, "telegram_bot_token", "TKN")
    monkeypatch.setattr(bot_main.settings, "internal_service_token", "svc-token")
    monkeypatch.setattr(bot_main, "hitl_ticket_repository", _StubHitlRepo())

    sent_dms: list[tuple[int, str]] = []

    async def fake_send_dm(chat_id: int, text: str) -> None:
        sent_dms.append((chat_id, text))

    monkeypatch.setattr(bot_main, "_send_dm", fake_send_dm)
    _stub_operator_lookup(monkeypatch, username=_OPERATOR)
    _stub_legacy_upsert(monkeypatch)

    # Drive the handler directly (bypassing the webhook so we can pass None).
    import asyncio

    from services.bot_gateway.app.calendar_commands import handle_calendar_command
    from services.bot_gateway.app.telegram_update import NormalizedTelegramMessage

    normalized = NormalizedTelegramMessage(
        update_id=1,
        source_message_id=1,
        chat_id=100,
        user_id=200,
        username="calendar_op",
        text="/calendar_service add x 60 mon 10:00-19:00",
    )

    async def fake_send_dm_direct(chat_id, text):
        sent_dms.append((chat_id, text))

    result = asyncio.get_event_loop().run_until_complete(
        handle_calendar_command(
            normalized=normalized,
            api_client=bot_main.api_client,
            send_dm=fake_send_dm_direct,
            internal_token="svc-token",
            nl_ops_db_path=None,
        )
    )
    assert result["decision"] == "added"
    assert all("устарела" not in dm[1] for dm in sent_dms)
