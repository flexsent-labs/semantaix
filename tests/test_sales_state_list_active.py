"""``StateRepository.list_active`` — read surface for ``/sales_state``.

Story 12.02 adds a list method that powers the operator's ``/sales_state``
command. Active = not in the terminal ``dormant`` stage. Optional ``chat_id``
filters server-side so a single-chat lookup doesn't have to fetch + scan
the whole project's state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from services.api.app.sales.intent import Intent
from services.api.app.sales.state_repository import StateRepository

_NOW = datetime(2026, 5, 27, 18, 42, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> StateRepository:
    return StateRepository(db_path=str(tmp_path / "sales.sqlite3"))


def test_list_active_empty_project_returns_empty(repo: StateRepository) -> None:
    assert repo.list_active(project_id=1) == []


def test_list_active_returns_all_for_project(repo: StateRepository) -> None:
    repo.upsert(
        chat_id=11,
        project_id=1,
        current_stage="scoping",
        collected_intent=Intent(dates="1 мая").to_dict(),
        now=_NOW,
        last_customer_msg_at=_NOW,
    )
    repo.upsert(
        chat_id=22,
        project_id=1,
        current_stage="proposing",
        collected_intent=Intent().to_dict(),
        now=_NOW,
    )
    # Different project — must NOT leak across.
    repo.upsert(
        chat_id=33,
        project_id=2,
        current_stage="scoping",
        collected_intent=Intent().to_dict(),
        now=_NOW,
    )
    rows = repo.list_active(project_id=1)
    assert sorted(r["chat_id"] for r in rows) == [11, 22]


def test_list_active_excludes_dormant(repo: StateRepository) -> None:
    repo.upsert(
        chat_id=11,
        project_id=1,
        current_stage="scoping",
        collected_intent=Intent().to_dict(),
        now=_NOW,
    )
    repo.upsert(
        chat_id=22,
        project_id=1,
        current_stage="dormant",
        collected_intent=Intent().to_dict(),
        now=_NOW,
    )
    rows = repo.list_active(project_id=1)
    assert [r["chat_id"] for r in rows] == [11]


def test_list_active_with_chat_id_filters_server_side(
    repo: StateRepository,
) -> None:
    repo.upsert(
        chat_id=11,
        project_id=1,
        current_stage="scoping",
        collected_intent=Intent().to_dict(),
        now=_NOW,
    )
    repo.upsert(
        chat_id=22,
        project_id=1,
        current_stage="scoping",
        collected_intent=Intent().to_dict(),
        now=_NOW,
    )
    rows = repo.list_active(project_id=1, chat_id=22)
    assert [r["chat_id"] for r in rows] == [22]


def test_list_active_with_chat_id_no_match_returns_empty(
    repo: StateRepository,
) -> None:
    repo.upsert(
        chat_id=11,
        project_id=1,
        current_stage="scoping",
        collected_intent=Intent().to_dict(),
        now=_NOW,
    )
    assert repo.list_active(project_id=1, chat_id=999) == []


def test_list_active_returns_intent_and_timestamps(
    repo: StateRepository,
) -> None:
    """Each row carries the same shape ``get`` returns so a single map function
    can render either path."""
    repo.upsert(
        chat_id=11,
        project_id=1,
        current_stage="scoping",
        collected_intent=Intent(dates="1 мая").to_dict(),
        now=_NOW,
        last_customer_msg_at=_NOW,
        last_bot_msg_at=_NOW,
    )
    [row] = repo.list_active(project_id=1)
    assert row["current_stage"] == "scoping"
    assert row["collected_intent"] == Intent(dates="1 мая").to_dict()
    assert row["last_customer_msg_at"] == _NOW
    assert row["last_bot_msg_at"] == _NOW
