"""Unit tests for ``ClientMaterialsRepository``.

Story 12.01 ships the full surface: ``add``, ``list_active``, ``get``,
``pick_by_tags`` (overlap-ranked), ``update_telegram_file_id``,
``soft_delete``, plus the frozen ``ClientMaterial`` dataclass.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from services.api.app.sales.client_materials_repository import (
    ClientMaterial,
    ClientMaterialsRepository,
    init_schema,
)

_NOW = datetime(2026, 5, 26, 10, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> ClientMaterialsRepository:
    return ClientMaterialsRepository(db_path=str(tmp_path / "sales.sqlite3"))


def test_add_returns_autoincrement_id(repo: ClientMaterialsRepository) -> None:
    first = repo.add(
        project_id=1,
        kind="pdf",
        local_path="/data/sales/x.pdf",
        byte_size=2048,
        now=_NOW,
    )
    second = repo.add(
        project_id=1,
        kind="photo",
        local_path="/data/sales/y.jpg",
        byte_size=4096,
        now=_NOW,
    )
    assert first == 1
    assert second == 2


def test_add_persists_all_optional_fields(
    repo: ClientMaterialsRepository, tmp_path: Path
) -> None:
    material_id = repo.add(
        project_id=7,
        kind="pdf",
        local_path="/data/sales/catalog.pdf",
        byte_size=12345,
        caption="Каталог туров",
        tags=["catalog", "tour"],
        source_operator_file_id="ABCDEFGH",
        telegram_file_id=None,
        now=_NOW,
    )
    # Read back via raw sqlite to verify columns persisted exactly.
    import sqlite3

    with sqlite3.connect(str(tmp_path / "sales.sqlite3")) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM client_materials WHERE id = ?", (material_id,)
        ).fetchone()
    assert row is not None
    assert int(row["project_id"]) == 7
    assert str(row["kind"]) == "pdf"
    assert str(row["local_path"]) == "/data/sales/catalog.pdf"
    assert int(row["byte_size"]) == 12345
    assert str(row["caption"]) == "Каталог туров"
    assert str(row["source_operator_file_id"]) == "ABCDEFGH"
    assert row["telegram_file_id"] is None
    assert int(row["is_active"]) == 1
    # tags stored as JSON.
    import json

    assert json.loads(row["tags_json"]) == ["catalog", "tour"]
    assert str(row["created_at"]) == _NOW.isoformat()
    assert str(row["updated_at"]) == _NOW.isoformat()


def test_add_defaults_optional_fields_to_null_and_empty(
    repo: ClientMaterialsRepository, tmp_path: Path
) -> None:
    material_id = repo.add(
        project_id=3,
        kind="document",
        local_path="/data/sales/x.bin",
        byte_size=1,
        now=_NOW,
    )
    import sqlite3

    with sqlite3.connect(str(tmp_path / "sales.sqlite3")) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM client_materials WHERE id = ?", (material_id,)
        ).fetchone()
    assert row["caption"] is None
    assert row["telegram_file_id"] is None
    assert row["source_operator_file_id"] is None
    assert row["duration_seconds"] is None
    import json

    assert json.loads(row["tags_json"]) == []


def test_init_schema_is_idempotent(tmp_path: Path) -> None:
    db_path = str(tmp_path / "sales.sqlite3")
    init_schema(db_path)
    init_schema(db_path)
    # The second call must not raise. Verify the table still exists.
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "client_materials" in names


def test_add_rejects_naive_datetime(repo: ClientMaterialsRepository) -> None:
    naive = datetime(2026, 5, 26, 10, 0)
    with pytest.raises(ValueError):
        repo.add(
            project_id=1,
            kind="pdf",
            local_path="/data/sales/x.pdf",
            byte_size=10,
            now=naive,
        )


def test_list_active_returns_only_active_for_project(
    repo: ClientMaterialsRepository,
) -> None:
    a = repo.add(
        project_id=1,
        kind="pdf",
        local_path="/x.pdf",
        byte_size=10,
        now=_NOW,
    )
    b = repo.add(
        project_id=1,
        kind="photo",
        local_path="/y.jpg",
        byte_size=20,
        now=_NOW,
    )
    # Different project — must not leak.
    repo.add(
        project_id=2,
        kind="pdf",
        local_path="/z.pdf",
        byte_size=30,
        now=_NOW,
    )
    rows = repo.list_active(project_id=1)
    ids = [row.id for row in rows]
    assert ids == [a, b]
    assert all(isinstance(row, ClientMaterial) for row in rows)


def test_list_active_excludes_soft_deleted(
    repo: ClientMaterialsRepository,
) -> None:
    a = repo.add(
        project_id=1,
        kind="pdf",
        local_path="/x.pdf",
        byte_size=10,
        now=_NOW,
    )
    b = repo.add(
        project_id=1,
        kind="photo",
        local_path="/y.jpg",
        byte_size=20,
        now=_NOW,
    )
    repo.soft_delete(material_id=a)
    rows = repo.list_active(project_id=1)
    assert [row.id for row in rows] == [b]


def test_get_returns_row_by_id(repo: ClientMaterialsRepository) -> None:
    mid = repo.add(
        project_id=1,
        kind="pdf",
        local_path="/x.pdf",
        byte_size=12,
        caption="кат",
        tags=["catalog"],
        source_operator_file_id="OPSHORT01",
        telegram_file_id=None,
        now=_NOW,
    )
    row = repo.get(material_id=mid)
    assert row is not None
    assert row.id == mid
    assert row.project_id == 1
    assert row.kind == "pdf"
    assert row.local_path == "/x.pdf"
    assert row.byte_size == 12
    assert row.caption == "кат"
    assert row.tags == ["catalog"]
    assert row.source_operator_file_id == "OPSHORT01"
    assert row.telegram_file_id is None
    assert row.is_active is True


def test_get_returns_none_for_unknown(repo: ClientMaterialsRepository) -> None:
    assert repo.get(material_id=99999) is None


def test_source_operator_file_id_persists_round_trip(
    repo: ClientMaterialsRepository,
) -> None:
    mid = repo.add(
        project_id=1,
        kind="pdf",
        local_path="/x.pdf",
        byte_size=12,
        source_operator_file_id="OPSHORT99",
        now=_NOW,
    )
    row = repo.get(material_id=mid)
    assert row is not None
    assert row.source_operator_file_id == "OPSHORT99"


def test_pick_by_tags_returns_overlap_only(
    repo: ClientMaterialsRepository,
) -> None:
    repo.add(
        project_id=1,
        kind="pdf",
        local_path="/x.pdf",
        byte_size=1,
        tags=["catalog", "rafting"],
        now=_NOW,
    )
    repo.add(
        project_id=1,
        kind="photo",
        local_path="/y.jpg",
        byte_size=1,
        tags=["unrelated"],
        now=_NOW,
    )
    matches = repo.pick_by_tags(project_id=1, tags=["catalog"])
    assert len(matches) == 1
    assert matches[0].local_path == "/x.pdf"


def test_pick_by_tags_empty_when_no_overlap(
    repo: ClientMaterialsRepository,
) -> None:
    repo.add(
        project_id=1,
        kind="pdf",
        local_path="/x.pdf",
        byte_size=1,
        tags=["catalog"],
        now=_NOW,
    )
    assert repo.pick_by_tags(project_id=1, tags=["unmatched"]) == []


def test_pick_by_tags_ranks_most_specific_first(
    repo: ClientMaterialsRepository,
) -> None:
    """When multiple rows overlap, the row with the most overlapping tags
    ranks first (most-specific first)."""
    one_match = repo.add(
        project_id=1,
        kind="pdf",
        local_path="/x.pdf",
        byte_size=1,
        tags=["catalog"],
        now=_NOW,
    )
    two_match = repo.add(
        project_id=1,
        kind="pdf",
        local_path="/y.pdf",
        byte_size=1,
        tags=["catalog", "rafting"],
        now=_NOW,
    )
    matches = repo.pick_by_tags(project_id=1, tags=["catalog", "rafting"])
    assert [row.id for row in matches] == [two_match, one_match]


def test_pick_by_tags_excludes_soft_deleted(
    repo: ClientMaterialsRepository,
) -> None:
    mid = repo.add(
        project_id=1,
        kind="pdf",
        local_path="/x.pdf",
        byte_size=1,
        tags=["catalog"],
        now=_NOW,
    )
    repo.soft_delete(material_id=mid)
    assert repo.pick_by_tags(project_id=1, tags=["catalog"]) == []


def test_pick_by_tags_empty_input_short_circuits(
    repo: ClientMaterialsRepository,
) -> None:
    repo.add(
        project_id=1,
        kind="pdf",
        local_path="/x.pdf",
        byte_size=1,
        tags=["catalog"],
        now=_NOW,
    )
    assert repo.pick_by_tags(project_id=1, tags=[]) == []


def test_pick_by_tags_excludes_other_projects(
    repo: ClientMaterialsRepository,
) -> None:
    repo.add(
        project_id=2,
        kind="pdf",
        local_path="/other.pdf",
        byte_size=1,
        tags=["catalog"],
        now=_NOW,
    )
    assert repo.pick_by_tags(project_id=1, tags=["catalog"]) == []


def test_update_telegram_file_id_updates_only_that_column(
    repo: ClientMaterialsRepository, tmp_path: Path
) -> None:
    mid = repo.add(
        project_id=1,
        kind="pdf",
        local_path="/x.pdf",
        byte_size=12,
        caption="cap",
        tags=["a"],
        now=_NOW,
    )
    repo.update_telegram_file_id(
        material_id=mid, telegram_file_id="TG-123"
    )
    row = repo.get(material_id=mid)
    assert row is not None
    assert row.telegram_file_id == "TG-123"
    # Sanity: other columns intact.
    assert row.caption == "cap"
    assert row.tags == ["a"]
    assert row.is_active is True


def test_soft_delete_removes_from_list_active(
    repo: ClientMaterialsRepository,
) -> None:
    mid = repo.add(
        project_id=1,
        kind="pdf",
        local_path="/x.pdf",
        byte_size=1,
        now=_NOW,
    )
    repo.soft_delete(material_id=mid)
    assert repo.list_active(project_id=1) == []
    # ``get`` returns the row regardless of ``is_active`` so callers can audit.
    row = repo.get(material_id=mid)
    assert row is not None
    assert row.is_active is False


def test_client_material_dataclass_is_frozen() -> None:
    """Mirrors the Epic-11 frozen-dataclass discipline so callers can't
    mutate retrieved rows in-place."""
    instance = ClientMaterial(
        id=1,
        project_id=1,
        kind="pdf",
        telegram_file_id=None,
        local_path="/x.pdf",
        byte_size=1,
        duration_seconds=None,
        caption=None,
        tags=[],
        source_operator_file_id=None,
        is_active=True,
        created_at=_NOW.isoformat(),
        updated_at=_NOW.isoformat(),
    )
    with pytest.raises(Exception):
        instance.kind = "video"  # type: ignore[misc]
