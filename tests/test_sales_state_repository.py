"""Unit tests for `StateRepository`.

Round-trip + invariants plus the Story 12.01 surface
(transition_stage, mark_customer_msg, mark_bot_msg, StateNotFound).
"""

from __future__ import annotations

from datetime import UTC, datetime, timezone
from pathlib import Path

import pytest

from services.api.app.sales.intent import Intent
from services.api.app.sales.state_repository import StateNotFound, StateRepository

_AWARE_NOW = datetime(2026, 5, 1, 13, 33, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> StateRepository:
    return StateRepository(db_path=str(tmp_path / "sales.sqlite3"))


def test_get_returns_none_for_missing_chat(repo: StateRepository) -> None:
    assert repo.get(42) is None


def test_upsert_round_trip(repo: StateRepository) -> None:
    repo.upsert(
        chat_id=7,
        project_id=1,
        current_stage="scoping",
        collected_intent=Intent(dates="1 мая").to_dict(),
        now=_AWARE_NOW,
    )
    state = repo.get(7)
    assert state is not None
    assert state["chat_id"] == 7
    assert state["project_id"] == 1
    assert state["current_stage"] == "scoping"
    assert state["collected_intent"] == Intent(dates="1 мая").to_dict()
    assert state["last_proposal"] is None


def test_upsert_overwrites_existing_row(repo: StateRepository) -> None:
    repo.upsert(
        chat_id=7,
        project_id=1,
        current_stage="new",
        collected_intent=Intent().to_dict(),
        now=_AWARE_NOW,
    )
    repo.upsert(
        chat_id=7,
        project_id=1,
        current_stage="scoping",
        collected_intent=Intent(dates="1 мая").to_dict(),
        now=_AWARE_NOW,
    )
    state = repo.get(7)
    assert state is not None
    assert state["current_stage"] == "scoping"
    assert state["collected_intent"] == Intent(dates="1 мая").to_dict()


def test_upsert_stores_bot_msg_timestamp(repo: StateRepository) -> None:
    repo.upsert(
        chat_id=7,
        project_id=1,
        current_stage="scoping",
        collected_intent=Intent().to_dict(),
        now=_AWARE_NOW,
        last_bot_msg_at=_AWARE_NOW,
    )
    state = repo.get(7)
    assert state is not None
    assert state["last_bot_msg_at"] == _AWARE_NOW.astimezone(UTC)


def test_upsert_stores_last_proposal_json(repo: StateRepository) -> None:
    repo.upsert(
        chat_id=7,
        project_id=1,
        current_stage="pitching",
        collected_intent=Intent().to_dict(),
        last_proposal={"service": "horna-river", "price": 15000},
        now=_AWARE_NOW,
    )
    state = repo.get(7)
    assert state is not None
    assert state["last_proposal"] == {"service": "horna-river", "price": 15000}


def test_upsert_preserves_prior_timestamps_when_omitted(
    repo: StateRepository,
) -> None:
    """An upsert that omits `last_bot_msg_at` must NOT clobber the existing
    one with NULL — the answerer can update other columns without losing
    the timestamp."""
    repo.upsert(
        chat_id=7,
        project_id=1,
        current_stage="scoping",
        collected_intent=Intent().to_dict(),
        now=_AWARE_NOW,
        last_bot_msg_at=_AWARE_NOW,
    )
    later = datetime(2026, 5, 1, 14, 0, tzinfo=UTC)
    repo.upsert(
        chat_id=7,
        project_id=1,
        current_stage="scoping",
        collected_intent=Intent(dates="1 мая").to_dict(),
        now=later,
        # last_bot_msg_at intentionally omitted
    )
    state = repo.get(7)
    assert state is not None
    assert state["last_bot_msg_at"] == _AWARE_NOW.astimezone(UTC)


def test_init_schema_idempotent(tmp_path: Path) -> None:
    db = str(tmp_path / "sales.sqlite3")
    a = StateRepository(db_path=db)
    a.upsert(
        chat_id=7,
        project_id=1,
        current_stage="scoping",
        collected_intent=Intent().to_dict(),
        now=_AWARE_NOW,
    )
    # Re-opening must not wipe rows or error.
    b = StateRepository(db_path=db)
    state = b.get(7)
    assert state is not None
    assert state["current_stage"] == "scoping"


def test_upsert_rejects_naive_datetime(repo: StateRepository) -> None:
    naive = datetime(2026, 5, 1, 13, 33)  # no tzinfo
    with pytest.raises(ValueError):
        repo.upsert(
            chat_id=7,
            project_id=1,
            current_stage="scoping",
            collected_intent=Intent().to_dict(),
            now=naive,
        )


def test_aware_non_utc_datetime_is_stored_as_utc(repo: StateRepository) -> None:
    pacific = timezone(offset=-datetime.now().astimezone().utcoffset())
    aware = datetime(2026, 5, 1, 6, 33, tzinfo=pacific)
    repo.upsert(
        chat_id=7,
        project_id=1,
        current_stage="scoping",
        collected_intent=Intent().to_dict(),
        now=aware,
        last_bot_msg_at=aware,
    )
    state = repo.get(7)
    assert state is not None
    stored = state["last_bot_msg_at"]
    assert stored is not None
    assert stored == aware.astimezone(UTC)


def test_transition_stage_updates_current_stage(repo: StateRepository) -> None:
    repo.upsert(
        chat_id=7,
        project_id=1,
        current_stage="new",
        collected_intent=Intent().to_dict(),
        now=_AWARE_NOW,
    )
    later = datetime(2026, 5, 1, 14, 0, tzinfo=UTC)
    repo.transition_stage(chat_id=7, new_stage="scoping", now=later)
    state = repo.get(7)
    assert state is not None
    assert state["current_stage"] == "scoping"


def test_transition_stage_missing_chat_raises(repo: StateRepository) -> None:
    with pytest.raises(StateNotFound):
        repo.transition_stage(chat_id=999, new_stage="scoping", now=_AWARE_NOW)


def test_mark_customer_msg_updates_only_customer_timestamp(
    repo: StateRepository,
) -> None:
    bot_time = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    repo.upsert(
        chat_id=7,
        project_id=1,
        current_stage="scoping",
        collected_intent=Intent().to_dict(),
        now=_AWARE_NOW,
        last_bot_msg_at=bot_time,
    )
    customer_time = datetime(2026, 5, 1, 13, 0, tzinfo=UTC)
    repo.mark_customer_msg(chat_id=7, now=customer_time)
    state = repo.get(7)
    assert state is not None
    assert state["last_customer_msg_at"] == customer_time
    assert state["last_bot_msg_at"] == bot_time


def test_mark_bot_msg_updates_only_bot_timestamp(repo: StateRepository) -> None:
    customer_time = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    repo.upsert(
        chat_id=7,
        project_id=1,
        current_stage="scoping",
        collected_intent=Intent().to_dict(),
        now=_AWARE_NOW,
        last_customer_msg_at=customer_time,
    )
    bot_time = datetime(2026, 5, 1, 14, 0, tzinfo=UTC)
    repo.mark_bot_msg(chat_id=7, now=bot_time)
    state = repo.get(7)
    assert state is not None
    assert state["last_bot_msg_at"] == bot_time
    assert state["last_customer_msg_at"] == customer_time


def test_mark_customer_msg_missing_chat_raises(repo: StateRepository) -> None:
    with pytest.raises(StateNotFound):
        repo.mark_customer_msg(chat_id=999, now=_AWARE_NOW)


def test_mark_bot_msg_missing_chat_raises(repo: StateRepository) -> None:
    with pytest.raises(StateNotFound):
        repo.mark_bot_msg(chat_id=999, now=_AWARE_NOW)


def test_transition_stage_rejects_naive_datetime(repo: StateRepository) -> None:
    repo.upsert(
        chat_id=7,
        project_id=1,
        current_stage="new",
        collected_intent=Intent().to_dict(),
        now=_AWARE_NOW,
    )
    naive = datetime(2026, 5, 1, 14, 0)
    with pytest.raises(ValueError):
        repo.transition_stage(chat_id=7, new_stage="scoping", now=naive)
