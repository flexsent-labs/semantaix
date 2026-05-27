"""``maybe_cancel`` cancels pending follow-ups for a chat (Story 12.08)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.api.app.sales.followup_cancel_hook import maybe_cancel
from services.api.app.sales.followup_queue_repository import (
    STATUS_CANCELLED_REPLIED,
    STATUS_SENT,
    STATUS_SKIPPED_STALE,
    FollowupQueueRepository,
)

_NOW = datetime(2026, 5, 26, 10, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> FollowupQueueRepository:
    return FollowupQueueRepository(db_path=str(tmp_path / "sales.sqlite3"))


def test_cancels_scheduled_row(repo: FollowupQueueRepository) -> None:
    row_id = repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW + timedelta(hours=24), now=_NOW
    )
    cancelled = maybe_cancel(repo=repo, chat_id=42, now=_NOW, trace_id="t")
    assert cancelled == 1
    row = repo.get(row_id)
    assert row is not None
    assert row.status == STATUS_CANCELLED_REPLIED


def test_no_op_when_chat_id_is_none(repo: FollowupQueueRepository) -> None:
    assert maybe_cancel(repo=repo, chat_id=None, now=_NOW) == 0


def test_does_not_touch_sent_row(repo: FollowupQueueRepository) -> None:
    row_id = repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW, now=_NOW
    )
    repo.mark_sent(row_id, now=_NOW)
    cancelled = maybe_cancel(repo=repo, chat_id=42, now=_NOW)
    assert cancelled == 0
    row = repo.get(row_id)
    assert row is not None
    assert row.status == STATUS_SENT


def test_does_not_touch_skipped_stale_row(
    repo: FollowupQueueRepository,
) -> None:
    row_id = repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW, now=_NOW
    )
    repo.mark_skipped_stale(row_id, reason="past_intent_date", now=_NOW)
    cancelled = maybe_cancel(repo=repo, chat_id=42, now=_NOW)
    assert cancelled == 0
    row = repo.get(row_id)
    assert row is not None
    assert row.status == STATUS_SKIPPED_STALE


def test_no_op_when_no_pending_rows(repo: FollowupQueueRepository) -> None:
    assert maybe_cancel(repo=repo, chat_id=42, now=_NOW) == 0
