"""Story 12.103 — per-user sliding-window rate limiter repository.

Covers:
- First message is always allowed.
- Messages up to the limit are allowed.
- The message at max+1 is rejected.
- Window expiry resets the counter.
- Different chat_ids are isolated.
- A rejected message does not increment the count (still rejected next call).
- Count persists across re-instantiation (restart parity).
- Only the expected table is created in the DB.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from services.bot_gateway.app.rate_limit_repository import InboundRateLimitRepository


def _now() -> datetime:
    return datetime.now(UTC)


def test_first_message_allowed(tmp_path):
    repo = InboundRateLimitRepository(db_path=str(tmp_path / "rl.sqlite3"))
    allowed = repo.check_and_record(
        chat_id=100, now=_now(), max_messages=10, window_seconds=300
    )
    assert allowed is True


def test_messages_up_to_limit_allowed(tmp_path):
    repo = InboundRateLimitRepository(db_path=str(tmp_path / "rl.sqlite3"))
    now = _now()
    for _ in range(10):
        assert (
            repo.check_and_record(
                chat_id=100, now=now, max_messages=10, window_seconds=300
            )
            is True
        )


def test_over_limit_rejected(tmp_path):
    repo = InboundRateLimitRepository(db_path=str(tmp_path / "rl.sqlite3"))
    now = _now()
    for _ in range(10):
        repo.check_and_record(chat_id=100, now=now, max_messages=10, window_seconds=300)
    assert (
        repo.check_and_record(chat_id=100, now=now, max_messages=10, window_seconds=300)
        is False
    )


def test_window_expiry_resets_count(tmp_path):
    repo = InboundRateLimitRepository(db_path=str(tmp_path / "rl.sqlite3"))
    old_now = _now()
    for _ in range(10):
        repo.check_and_record(
            chat_id=100, now=old_now, max_messages=10, window_seconds=300
        )
    # Advance past the window boundary
    new_now = old_now + timedelta(seconds=301)
    assert (
        repo.check_and_record(
            chat_id=100, now=new_now, max_messages=10, window_seconds=300
        )
        is True
    )


def test_chat_ids_isolated(tmp_path):
    repo = InboundRateLimitRepository(db_path=str(tmp_path / "rl.sqlite3"))
    now = _now()
    for _ in range(10):
        repo.check_and_record(chat_id=100, now=now, max_messages=10, window_seconds=300)
    # A different chat_id starts a fresh window
    assert (
        repo.check_and_record(chat_id=200, now=now, max_messages=10, window_seconds=300)
        is True
    )


def test_rejected_does_not_increment_count(tmp_path):
    """Rejected messages do not push the count higher; the window still resets correctly."""
    repo = InboundRateLimitRepository(db_path=str(tmp_path / "rl.sqlite3"))
    now = _now()
    for _ in range(10):
        repo.check_and_record(chat_id=100, now=now, max_messages=10, window_seconds=300)
    # Rejections
    assert (
        repo.check_and_record(chat_id=100, now=now, max_messages=10, window_seconds=300)
        is False
    )
    assert (
        repo.check_and_record(chat_id=100, now=now, max_messages=10, window_seconds=300)
        is False
    )
    # After window expires the count resets
    new_now = now + timedelta(seconds=301)
    assert (
        repo.check_and_record(
            chat_id=100, now=new_now, max_messages=10, window_seconds=300
        )
        is True
    )


def test_survives_reinit_same_db(tmp_path):
    """Count persists across re-instantiation (restart parity)."""
    db = str(tmp_path / "rl.sqlite3")
    now = _now()
    repo1 = InboundRateLimitRepository(db_path=db)
    for _ in range(10):
        repo1.check_and_record(chat_id=100, now=now, max_messages=10, window_seconds=300)
    repo2 = InboundRateLimitRepository(db_path=db)
    assert (
        repo2.check_and_record(chat_id=100, now=now, max_messages=10, window_seconds=300)
        is False
    )


def test_dedicated_table_only(tmp_path):
    """Only inbound_request_counts exists — no transcript or unrelated tables."""
    db_path = tmp_path / "rl.sqlite3"
    repo = InboundRateLimitRepository(db_path=str(db_path))
    repo.check_and_record(chat_id=100, now=_now(), max_messages=10, window_seconds=300)
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert tables == {"inbound_request_counts"}
