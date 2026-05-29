from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from services.bot_gateway.app.offline_backlog_buffer import (
    BufferedMessage,
    OfflineBacklogBuffer,
)


def _make_buffer(tmp_path: Path) -> OfflineBacklogBuffer:
    return OfflineBacklogBuffer(db_path=str(tmp_path / "backlog.db"))


def test_first_add_returns_true_subsequent_returns_false(tmp_path: Path) -> None:
    buf = _make_buffer(tmp_path)
    first = buf.add(
        chat_id=10,
        update_id=100,
        source_message_id=200,
        customer_username="@c",
        text="привет",
        message_date=1710000000,
    )
    second = buf.add(
        chat_id=10,
        update_id=101,
        source_message_id=201,
        customer_username="@c",
        text="ещё",
        message_date=1710000001,
    )
    assert first is True
    assert second is False


def test_duplicate_update_id_does_not_double_insert(tmp_path: Path) -> None:
    buf = _make_buffer(tmp_path)
    first = buf.add(
        chat_id=10,
        update_id=100,
        source_message_id=200,
        customer_username="@c",
        text="привет",
        message_date=1710000000,
    )
    again = buf.add(
        chat_id=10,
        update_id=100,
        source_message_id=200,
        customer_username="@c",
        text="привет",
        message_date=1710000000,
    )
    assert first is True
    assert again is False
    assert len(buf.drain(chat_id=10)) == 1


def test_drain_returns_messages_in_update_order_and_removes(tmp_path: Path) -> None:
    buf = _make_buffer(tmp_path)
    buf.add(
        chat_id=10,
        update_id=100,
        source_message_id=200,
        customer_username="@c",
        text="первое",
        message_date=1710000000,
    )
    buf.add(
        chat_id=10,
        update_id=101,
        source_message_id=201,
        customer_username=None,
        text="второе",
        message_date=None,
    )
    drained = buf.drain(chat_id=10)
    assert [m.text for m in drained] == ["первое", "второе"]
    assert [m.update_id for m in drained] == [100, 101]
    assert [m.source_message_id for m in drained] == [200, 201]
    assert [m.customer_username for m in drained] == ["@c", None]
    assert [m.message_date for m in drained] == [1710000000, None]
    assert drained[0].chat_id == 10
    assert isinstance(drained[0], BufferedMessage)
    assert buf.drain(chat_id=10) == []


def test_drain_missing_chat_returns_empty_list(tmp_path: Path) -> None:
    buf = _make_buffer(tmp_path)
    assert buf.drain(chat_id=999) == []


def test_independent_chats_do_not_interfere(tmp_path: Path) -> None:
    buf = _make_buffer(tmp_path)
    first_a = buf.add(
        chat_id=1,
        update_id=1,
        source_message_id=1,
        customer_username="@a",
        text="a",
        message_date=None,
    )
    first_b = buf.add(
        chat_id=2,
        update_id=2,
        source_message_id=2,
        customer_username="@b",
        text="b",
        message_date=None,
    )
    assert first_a is True
    assert first_b is True
    assert [m.text for m in buf.drain(chat_id=1)] == ["a"]
    assert [m.text for m in buf.drain(chat_id=2)] == ["b"]


def test_schema_initialised_on_construction(tmp_path: Path) -> None:
    db_path = tmp_path / "buf.db"
    OfflineBacklogBuffer(db_path=str(db_path))
    with sqlite3.connect(db_path) as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "offline_backlog_buffer" in names


def test_latest_received_at_returns_max(tmp_path: Path) -> None:
    buf = _make_buffer(tmp_path)
    buf.add(
        chat_id=10,
        update_id=1,
        source_message_id=1,
        customer_username="@c",
        text="a",
        message_date=None,
    )
    first = buf.latest_received_at(chat_id=10)
    assert first is not None
    buf.add(
        chat_id=10,
        update_id=2,
        source_message_id=2,
        customer_username="@c",
        text="b",
        message_date=None,
    )
    second = buf.latest_received_at(chat_id=10)
    assert second is not None
    assert second >= first


def test_latest_received_at_none_for_unknown_chat(tmp_path: Path) -> None:
    buf = _make_buffer(tmp_path)
    assert buf.latest_received_at(chat_id=404) is None


def test_latest_received_at_none_after_drain(tmp_path: Path) -> None:
    buf = _make_buffer(tmp_path)
    buf.add(
        chat_id=10,
        update_id=1,
        source_message_id=1,
        customer_username="@c",
        text="a",
        message_date=None,
    )
    buf.drain(chat_id=10)
    assert buf.latest_received_at(chat_id=10) is None


def test_pending_chat_ids_lists_distinct_chats(tmp_path: Path) -> None:
    buf = _make_buffer(tmp_path)
    assert buf.pending_chat_ids() == []
    buf.add(
        chat_id=1,
        update_id=1,
        source_message_id=1,
        customer_username="@a",
        text="a",
        message_date=None,
    )
    buf.add(
        chat_id=1,
        update_id=2,
        source_message_id=2,
        customer_username="@a",
        text="b",
        message_date=None,
    )
    buf.add(
        chat_id=2,
        update_id=3,
        source_message_id=3,
        customer_username="@b",
        text="c",
        message_date=None,
    )
    assert sorted(buf.pending_chat_ids()) == [1, 2]
    buf.drain(chat_id=1)
    assert buf.pending_chat_ids() == [2]


@pytest.mark.asyncio
async def test_concurrent_add_only_one_returns_true(tmp_path: Path) -> None:
    import asyncio

    buf = _make_buffer(tmp_path)

    async def add(update_id: int) -> bool:
        return await asyncio.to_thread(
            buf.add,
            chat_id=77,
            update_id=update_id,
            source_message_id=update_id,
            customer_username="@c",
            text=f"m{update_id}",
            message_date=None,
        )

    results = await asyncio.gather(*(add(i) for i in range(8)))
    assert sum(1 for r in results if r) == 1
    assert len(buf.drain(chat_id=77)) == 8
