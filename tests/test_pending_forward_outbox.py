from __future__ import annotations

import pytest

from services.bot_gateway.app.pending_forward_outbox import (
    PendingForward,
    PendingForwardOutbox,
)


@pytest.fixture
def outbox(tmp_path) -> PendingForwardOutbox:
    return PendingForwardOutbox(str(tmp_path / "pending.sqlite3"))


def _mark(outbox: PendingForwardOutbox, chat_id: int, update_id: int, text: str) -> None:
    outbox.mark_pending(
        chat_id=chat_id,
        update_id=update_id,
        source_message_id=update_id,
        customer_username="@c",
        text=text,
        trace_id=f"tg-update-{update_id}",
    )


def test_mark_pending_then_peek_returns_row(outbox):
    _mark(outbox, 10, 1, "хочу забронировать багги завтра в 14:00")

    rows = outbox.peek_chat(chat_id=10)

    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, PendingForward)
    assert row.chat_id == 10
    assert row.update_id == 1
    assert row.source_message_id == 1
    assert row.customer_username == "@c"
    assert row.text == "хочу забронировать багги завтра в 14:00"
    assert row.trace_id == "tg-update-1"
    assert row.received_at  # non-empty ISO timestamp


def test_pending_chat_ids_lists_distinct_chats(outbox):
    _mark(outbox, 10, 1, "a")
    _mark(outbox, 10, 2, "b")
    _mark(outbox, 11, 3, "c")

    assert sorted(outbox.pending_chat_ids()) == [10, 11]


def test_peek_chat_is_ordered_by_update_id_and_non_destructive(outbox):
    _mark(outbox, 10, 3, "third")
    _mark(outbox, 10, 1, "first")
    _mark(outbox, 10, 2, "second")

    rows = outbox.peek_chat(chat_id=10)
    assert [r.text for r in rows] == ["first", "second", "third"]
    # Peek must not delete — a failed re-forward needs the rows to survive.
    assert len(outbox.peek_chat(chat_id=10)) == 3


def test_peek_unknown_chat_returns_empty(outbox):
    assert outbox.peek_chat(chat_id=999) == []


def test_duplicate_mark_is_idempotent(outbox):
    _mark(outbox, 10, 1, "original")
    _mark(outbox, 10, 1, "redelivered duplicate")

    rows = outbox.peek_chat(chat_id=10)
    assert len(rows) == 1
    # First write wins (INSERT OR IGNORE) — mirrors Telegram redelivery dedup.
    assert rows[0].text == "original"


def test_clear_removes_single_row_only(outbox):
    _mark(outbox, 10, 1, "a")
    _mark(outbox, 10, 2, "b")

    outbox.clear(chat_id=10, update_id=1)

    assert [r.update_id for r in outbox.peek_chat(chat_id=10)] == [2]


def test_clear_chat_removes_all_rows_for_chat(outbox):
    _mark(outbox, 10, 1, "a")
    _mark(outbox, 10, 2, "b")
    _mark(outbox, 11, 3, "c")

    outbox.clear_chat(chat_id=10)

    assert outbox.peek_chat(chat_id=10) == []
    assert [r.update_id for r in outbox.peek_chat(chat_id=11)] == [3]


def test_clear_unknown_row_is_noop(outbox):
    _mark(outbox, 10, 1, "a")

    outbox.clear(chat_id=10, update_id=999)
    outbox.clear_chat(chat_id=12345)

    assert len(outbox.peek_chat(chat_id=10)) == 1
