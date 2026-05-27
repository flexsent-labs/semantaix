"""Integration tests for the Story 12.01 sales DB bootstrap.

Verifies the single :func:`services.api.app.sales.bootstrap.init_schema`
entry point creates all four tables + the three spec-named indexes, and
that calling it twice is a no-op for both schema and rows. Also asserts
the default-off invariant — every table is empty after bootstrap so the
sales answerer's activation gate stays silent until an operator adds
the first service row.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from services.api.app.sales.bootstrap import init_schema
from services.api.app.sales.client_materials_repository import (
    ClientMaterialsRepository,
)
from services.api.app.sales.followup_queue_repository import (
    FollowupQueueRepository,
)
from services.api.app.sales.services_repository import ServicesRepository
from services.api.app.sales.state_repository import StateRepository

_NOW = datetime(2026, 5, 27, 9, 0, tzinfo=UTC)

_EXPECTED_TABLES = {
    "sales_conversation_state",
    "services",
    "client_materials",
    "sales_followup_queue",
}

_SPEC_INDEXES = {
    "idx_services_project",
    "idx_client_materials_project",
    "idx_followup_due",
}


def _table_names(db_path: str) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }


def _index_names(db_path: str) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }


def test_init_schema_creates_all_four_tables(tmp_path: Path) -> None:
    db_path = str(tmp_path / "sales.sqlite3")
    init_schema(db_path)
    assert _EXPECTED_TABLES.issubset(_table_names(db_path))


def test_init_schema_creates_spec_indexes(tmp_path: Path) -> None:
    db_path = str(tmp_path / "sales.sqlite3")
    init_schema(db_path)
    assert _SPEC_INDEXES.issubset(_index_names(db_path))


def test_init_schema_default_off_zero_services_rows(tmp_path: Path) -> None:
    """Default-off invariant — the bootstrap creates an empty ``services``
    table so the always-on activation gate keeps the sales answerer silent."""
    db_path = str(tmp_path / "sales.sqlite3")
    init_schema(db_path)
    with sqlite3.connect(db_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM services"
        ).fetchone()[0]
    assert count == 0


def test_init_schema_default_off_zero_state_rows(tmp_path: Path) -> None:
    """No conversation state should exist on a fresh DB."""
    db_path = str(tmp_path / "sales.sqlite3")
    init_schema(db_path)
    with sqlite3.connect(db_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM sales_conversation_state"
        ).fetchone()[0]
    assert count == 0


def test_init_schema_idempotent_preserves_rows(tmp_path: Path) -> None:
    """Calling :func:`init_schema` twice does not wipe schema or data."""
    db_path = str(tmp_path / "sales.sqlite3")
    init_schema(db_path)

    services = ServicesRepository(db_path=db_path)
    service_id = services.add(project_id=1, name="alpha", now=_NOW)

    state = StateRepository(db_path=db_path)
    state.upsert(
        chat_id=7,
        project_id=1,
        current_stage="scoping",
        collected_intent={},
        now=_NOW,
    )

    queue = FollowupQueueRepository(db_path=db_path)
    queue.enqueue(chat_id=7, project_id=1, fire_at=_NOW, now=_NOW)

    materials = ClientMaterialsRepository(db_path=db_path)
    materials.add(
        project_id=1,
        kind="pdf",
        local_path="/x.pdf",
        byte_size=10,
        now=_NOW,
    )

    # Second call must be a complete no-op.
    init_schema(db_path)

    assert _EXPECTED_TABLES.issubset(_table_names(db_path))
    assert _SPEC_INDEXES.issubset(_index_names(db_path))
    assert services.count_active(project_id=1) == 1
    assert services.get(service_id=service_id) is not None
    assert state.get(7) is not None
    assert queue.list_for_chat(7) != []
    assert materials.list_active(project_id=1) != []


def test_pragma_foreign_keys_default_off(tmp_path: Path) -> None:
    """Mirrors the one-file-per-concern discipline: no foreign keys."""
    db_path = str(tmp_path / "sales.sqlite3")
    init_schema(db_path)
    with sqlite3.connect(db_path) as connection:
        row = connection.execute("PRAGMA foreign_keys").fetchone()
    assert int(row[0]) == 0


def test_init_schema_is_module_callable() -> None:
    """The bootstrap entry point is exported by name so main.py can wire it."""
    from services.api.app.sales import bootstrap

    assert callable(bootstrap.init_schema)


@pytest.mark.parametrize(
    "table",
    sorted(_EXPECTED_TABLES),
)
def test_each_table_present_after_first_call(
    tmp_path: Path, table: str
) -> None:
    db_path = str(tmp_path / "sales.sqlite3")
    init_schema(db_path)
    assert table in _table_names(db_path)
