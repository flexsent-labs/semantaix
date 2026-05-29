from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from platform_common.settings import get_settings
from services.bot_gateway.app import main as bot_main
from services.bot_gateway.app.main import api_client
from services.bot_gateway.app.main import app as bot_app
from services.bot_gateway.app.pending_forward_outbox import PendingForwardOutbox


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("PERSISTENCE_DB_PATH", str(tmp_path / "persistence.sqlite3"))
    get_settings.cache_clear()
    # The module singleton binds the real .data path at import; swap in a fresh,
    # isolated outbox per test so nothing touches production data.
    monkeypatch.setattr(
        bot_main,
        "pending_forward_outbox",
        PendingForwardOutbox(str(tmp_path / "pending.sqlite3")),
    )
    yield
    get_settings.cache_clear()


def _live_payload(*, update_id: int, message_id: int, text: str, chat_id: int = 7000) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": {"id": chat_id, "username": "customer"},
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
            "date": int(datetime.now(UTC).timestamp()),
        },
    }


def _seed(chat_id: int, *messages: tuple[int, str]) -> None:
    for update_id, text in messages:
        bot_main.pending_forward_outbox.mark_pending(
            chat_id=chat_id,
            update_id=update_id,
            source_message_id=update_id,
            customer_username="@c",
            text=text,
            trace_id=f"tg-update-{update_id}",
        )


# --- live path: mark + clear ------------------------------------------------


def test_live_message_clears_outbox_after_successful_forward(monkeypatch):
    forward = AsyncMock(return_value={})
    monkeypatch.setattr(api_client, "forward_inbound", forward)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_live_payload(update_id=9100, message_id=1, text="есть ли свободные места?"),
    )

    assert response.json()["status"] == "accepted"
    forward.assert_awaited_once()
    # Forward confirmed -> nothing left pending.
    assert bot_main.pending_forward_outbox.pending_chat_ids() == []


def test_live_message_keeps_pending_row_when_forward_fails(monkeypatch):
    forward = AsyncMock(side_effect=RuntimeError("api down"))
    monkeypatch.setattr(api_client, "forward_inbound", forward)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_live_payload(update_id=9101, message_id=2, text="хочу забронировать багги"),
    )

    # Webhook still 200s (failure is swallowed in the background task) ...
    assert response.json()["status"] == "accepted"
    # ... but the message survives in the outbox for the next restart to replay.
    rows = bot_main.pending_forward_outbox.peek_chat(chat_id=7000)
    assert [r.text for r in rows] == ["хочу забронировать багги"]


# --- startup recovery -------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_recovery_replays_latest_and_clears(monkeypatch):
    forward = AsyncMock(return_value={})
    monkeypatch.setattr(api_client, "forward_inbound", forward)
    _seed(70, (1, "хочу забронировать багги завтра в 14:00"))

    await bot_main._recover_pending_forwards_on_startup()
    await asyncio.gather(*list(bot_main._pending_forward_recovery_tasks))

    forward.assert_awaited_once()
    kwargs = forward.await_args.kwargs
    assert kwargs["text"] == "хочу забронировать багги завтра в 14:00"
    assert kwargs["chat_id"] == 70
    assert kwargs["customer_username"] == "@c"
    assert kwargs["trace_id"] == "tg-update-1"
    assert bot_main.pending_forward_outbox.pending_chat_ids() == []


@pytest.mark.asyncio
async def test_startup_recovery_collapses_multiple_to_latest_with_context(monkeypatch):
    forward = AsyncMock(return_value={})
    monkeypatch.setattr(api_client, "forward_inbound", forward)
    # Latest ("да") is a thin cue -> preceding pulled in as labeled context.
    _seed(71, (1, "хочу забронировать багги завтра в 14:00"), (2, "да"))

    await bot_main._recover_pending_forwards_on_startup()
    await asyncio.gather(*list(bot_main._pending_forward_recovery_tasks))

    forward.assert_awaited_once()
    text = forward.await_args.kwargs["text"]
    assert "хочу забронировать багги завтра в 14:00" in text
    assert text.rstrip().endswith("да")
    assert bot_main.pending_forward_outbox.pending_chat_ids() == []


@pytest.mark.asyncio
async def test_startup_recovery_independent_per_chat(monkeypatch):
    forward = AsyncMock(return_value={})
    monkeypatch.setattr(api_client, "forward_inbound", forward)
    _seed(72, (1, "первый клиент спрашивает про цену"))
    _seed(73, (2, "второй клиент спрашивает про время"))

    await bot_main._recover_pending_forwards_on_startup()
    await asyncio.gather(*list(bot_main._pending_forward_recovery_tasks))

    assert forward.await_count == 2
    assert bot_main.pending_forward_outbox.pending_chat_ids() == []


@pytest.mark.asyncio
async def test_startup_recovery_noop_when_outbox_empty(monkeypatch):
    forward = AsyncMock(return_value={})
    monkeypatch.setattr(api_client, "forward_inbound", forward)

    await bot_main._recover_pending_forwards_on_startup()

    forward.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_recovery_keeps_rows_when_reforward_fails(monkeypatch):
    forward = AsyncMock(side_effect=RuntimeError("still down"))
    monkeypatch.setattr(api_client, "forward_inbound", forward)
    _seed(74, (1, "сообщение которое всё ещё не доставлено"))

    await bot_main._recover_pending_forwards_on_startup()
    await asyncio.gather(*list(bot_main._pending_forward_recovery_tasks))

    # Re-forward failed again -> row stays for the next restart, not dropped.
    rows = bot_main.pending_forward_outbox.peek_chat(chat_id=74)
    assert [r.text for r in rows] == ["сообщение которое всё ещё не доставлено"]


@pytest.mark.asyncio
async def test_replay_empty_chat_is_noop(monkeypatch):
    # A concurrent drain can empty the chat between listing and peeking; the
    # replay must tolerate finding no rows.
    forward = AsyncMock(return_value={})
    monkeypatch.setattr(api_client, "forward_inbound", forward)

    await bot_main._replay_pending_forward(chat_id=404)

    forward.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_thin_latest_with_zero_max_context_drops_context(monkeypatch):
    forward = AsyncMock(return_value={})
    monkeypatch.setattr(api_client, "forward_inbound", forward)
    monkeypatch.setattr(bot_main.settings, "offline_backlog_max_context_messages", 0)
    _seed(75, (1, "длинное сообщение про бронирование багги"), (2, "да"))

    await bot_main._replay_pending_forward(chat_id=75)

    # max_context=0 -> latest forwarded with no preceding context block.
    assert forward.await_args.kwargs["text"] == "да"


@pytest.mark.asyncio
async def test_replay_swallows_unexpected_error(monkeypatch):
    forward = AsyncMock(return_value={})
    monkeypatch.setattr(api_client, "forward_inbound", forward)
    monkeypatch.setattr(
        bot_main, "build_inbound_text", Mock(side_effect=RuntimeError("boom"))
    )
    _seed(76, (1, "сообщение клиента"))

    # A replay failure must never crash startup; the error is logged + swallowed.
    await bot_main._replay_pending_forward(chat_id=76)

    forward.assert_not_awaited()
