"""Unit tests for ``FollowupQueueRepository`` (Story 12.08).

The story adds a ``reason`` column to ``sales_followup_queue`` and the
queue methods the api endpoints + scheduler job consume.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.api.app.sales.followup_queue_repository import (
    REASON_PAST_INTENT_DATE,
    REASON_TELEGRAM_SEND_FAILED,
    STATUS_CANCELLED_REPLACED,
    STATUS_CANCELLED_REPLIED,
    STATUS_SCHEDULED,
    STATUS_SENT,
    STATUS_SKIPPED_STALE,
    FollowupQueueRepository,
)

_NOW = datetime(2026, 5, 26, 10, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> FollowupQueueRepository:
    return FollowupQueueRepository(db_path=str(tmp_path / "sales.sqlite3"))


def test_enqueue_creates_scheduled_row(repo: FollowupQueueRepository) -> None:
    row_id = repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW + timedelta(hours=24), now=_NOW
    )
    row = repo.get(row_id)
    assert row is not None
    assert row.status == STATUS_SCHEDULED
    assert row.chat_id == 42
    assert row.project_id == 1
    assert row.reason is None
    assert row.fire_at == _NOW + timedelta(hours=24)


def test_enqueue_replaces_prior_scheduled_row(
    repo: FollowupQueueRepository,
) -> None:
    first = repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW + timedelta(hours=24), now=_NOW
    )
    second = repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW + timedelta(hours=25), now=_NOW
    )
    first_row = repo.get(first)
    second_row = repo.get(second)
    assert first_row is not None
    assert second_row is not None
    assert first_row.status == STATUS_CANCELLED_REPLACED
    assert second_row.status == STATUS_SCHEDULED


def test_enqueue_does_not_touch_sent_rows(
    repo: FollowupQueueRepository,
) -> None:
    row_id = repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW, now=_NOW
    )
    repo.mark_sent(row_id, now=_NOW)
    repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW + timedelta(hours=24), now=_NOW
    )
    sent_row = repo.get(row_id)
    assert sent_row is not None
    assert sent_row.status == STATUS_SENT


def test_due_returns_scheduled_rows_with_fire_at_in_past(
    repo: FollowupQueueRepository,
) -> None:
    past = repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW - timedelta(seconds=1), now=_NOW
    )
    future = repo.enqueue(
        chat_id=99, project_id=1, fire_at=_NOW + timedelta(hours=24), now=_NOW
    )
    due = repo.due(now=_NOW)
    ids = [row.id for row in due]
    assert past in ids
    assert future not in ids


def test_due_excludes_non_scheduled_rows(repo: FollowupQueueRepository) -> None:
    row_id = repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW - timedelta(hours=1), now=_NOW
    )
    repo.mark_sent(row_id, now=_NOW)
    assert repo.due(now=_NOW) == []


def test_due_respects_limit(repo: FollowupQueueRepository) -> None:
    for chat_id in range(5):
        repo.enqueue(
            chat_id=chat_id,
            project_id=1,
            fire_at=_NOW - timedelta(seconds=chat_id + 1),
            now=_NOW,
        )
    due = repo.due(now=_NOW, limit=2)
    assert len(due) == 2


def test_mark_sent_persists(repo: FollowupQueueRepository) -> None:
    row_id = repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW, now=_NOW
    )
    later = _NOW + timedelta(minutes=1)
    repo.mark_sent(row_id, now=later)
    row = repo.get(row_id)
    assert row is not None
    assert row.status == STATUS_SENT
    assert row.reason is None
    assert row.updated_at == later


def test_mark_skipped_stale_persists_reason(
    repo: FollowupQueueRepository,
) -> None:
    row_id = repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW, now=_NOW
    )
    repo.mark_skipped_stale(
        row_id, reason=REASON_PAST_INTENT_DATE, now=_NOW
    )
    row = repo.get(row_id)
    assert row is not None
    assert row.status == STATUS_SKIPPED_STALE
    assert row.reason == REASON_PAST_INTENT_DATE


def test_mark_skipped_stale_telegram_failure_reason(
    repo: FollowupQueueRepository,
) -> None:
    row_id = repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW, now=_NOW
    )
    repo.mark_skipped_stale(
        row_id, reason=REASON_TELEGRAM_SEND_FAILED, now=_NOW
    )
    row = repo.get(row_id)
    assert row is not None
    assert row.reason == REASON_TELEGRAM_SEND_FAILED


def test_reschedule_updates_fire_at_keeps_status_scheduled(
    repo: FollowupQueueRepository,
) -> None:
    row_id = repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW, now=_NOW
    )
    new_fire = _NOW + timedelta(hours=12)
    repo.reschedule(row_id, new_fire_at=new_fire, now=_NOW)
    row = repo.get(row_id)
    assert row is not None
    assert row.status == STATUS_SCHEDULED
    assert row.fire_at == new_fire


def test_reschedule_does_not_touch_non_scheduled(
    repo: FollowupQueueRepository,
) -> None:
    row_id = repo.enqueue(chat_id=42, project_id=1, fire_at=_NOW, now=_NOW)
    repo.mark_sent(row_id, now=_NOW)
    repo.reschedule(
        row_id, new_fire_at=_NOW + timedelta(days=1), now=_NOW
    )
    row = repo.get(row_id)
    assert row is not None
    assert row.status == STATUS_SENT
    assert row.fire_at == _NOW


def test_mark_cancelled_replied_targets_scheduled(
    repo: FollowupQueueRepository,
) -> None:
    scheduled = repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW + timedelta(hours=24), now=_NOW
    )
    sent_id = repo.enqueue(
        chat_id=99, project_id=1, fire_at=_NOW, now=_NOW
    )
    repo.mark_sent(sent_id, now=_NOW)

    cancelled_count = repo.mark_cancelled_replied(42, now=_NOW)
    assert cancelled_count == 1
    scheduled_row = repo.get(scheduled)
    assert scheduled_row is not None
    assert scheduled_row.status == STATUS_CANCELLED_REPLIED
    sent_row = repo.get(sent_id)
    assert sent_row is not None
    assert sent_row.status == STATUS_SENT


def test_mark_cancelled_replied_returns_zero_when_nothing_pending(
    repo: FollowupQueueRepository,
) -> None:
    assert repo.mark_cancelled_replied(42, now=_NOW) == 0


def test_list_for_chat_returns_all_history(
    repo: FollowupQueueRepository,
) -> None:
    first = repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW, now=_NOW
    )
    repo.mark_sent(first, now=_NOW)
    second = repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW + timedelta(hours=24), now=_NOW
    )
    rows = repo.list_for_chat(42)
    assert [r.id for r in rows] == [first, second]


def test_get_returns_none_for_missing(repo: FollowupQueueRepository) -> None:
    assert repo.get(99999) is None


def test_enqueue_rejects_naive_datetime(
    repo: FollowupQueueRepository,
) -> None:
    naive = datetime(2026, 5, 26, 10, 0)
    with pytest.raises(ValueError):
        repo.enqueue(chat_id=42, project_id=1, fire_at=naive, now=_NOW)


def test_init_schema_idempotent(tmp_path: Path) -> None:
    db = str(tmp_path / "sales.sqlite3")
    a = FollowupQueueRepository(db_path=db)
    a.enqueue(chat_id=42, project_id=1, fire_at=_NOW, now=_NOW)
    b = FollowupQueueRepository(db_path=db)
    rows = b.list_for_chat(42)
    assert len(rows) == 1
