"""Unit tests for the Story 12.02 ``ServicesRepository``.

Owns the ``services`` catalog table in ``.data/semantaix_sales.db``. The
surface is intentionally narrower than Epic 13's ``ProjectServiceRepository``
— Story 12.02 only needs name + description_md + tags + is_active per
``(project_id, lower(name))``. The schema is set up at construction so the
bootstrap is idempotent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from services.api.app.sales.services_repository import (
    ServiceAlreadyExists,
    ServiceNotFound,
    ServicesRepository,
)

_NOW = datetime(2026, 5, 27, 9, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> ServicesRepository:
    return ServicesRepository(db_path=str(tmp_path / "sales.sqlite3"))


def test_add_returns_id_and_persists_row(repo: ServicesRepository) -> None:
    service_id = repo.add(
        project_id=1,
        name="каньонинг",
        description_md="Каньонинг — это…",
        tags=["adventure"],
        now=_NOW,
    )
    assert service_id > 0
    rows = repo.list_active(project_id=1)
    assert len(rows) == 1
    row = rows[0]
    assert row.id == service_id
    assert row.project_id == 1
    assert row.name == "каньонинг"
    assert row.description_md == "Каньонинг — это…"
    assert row.tags == ["adventure"]
    assert row.is_active is True


def test_add_name_only_persists_with_null_optional_columns(
    repo: ServicesRepository,
) -> None:
    service_id = repo.add(project_id=2, name="trek", now=_NOW)
    rows = repo.list_active(project_id=2)
    assert len(rows) == 1
    assert rows[0].id == service_id
    assert rows[0].description_md is None
    assert rows[0].tags == []


def test_add_rejects_duplicate_per_project_case_insensitive(
    repo: ServicesRepository,
) -> None:
    repo.add(project_id=1, name="каньонинг", now=_NOW)
    with pytest.raises(ServiceAlreadyExists) as exc:
        repo.add(project_id=1, name="КАНЬОНИНГ", now=_NOW)
    assert "каньонинг" in str(exc.value).lower()


def test_add_same_name_different_project_allowed(
    repo: ServicesRepository,
) -> None:
    repo.add(project_id=1, name="каньонинг", now=_NOW)
    other_id = repo.add(project_id=2, name="каньонинг", now=_NOW)
    assert other_id > 0


def test_list_active_returns_only_active(repo: ServicesRepository) -> None:
    a = repo.add(project_id=1, name="alpha", now=_NOW)
    b = repo.add(project_id=1, name="beta", now=_NOW)
    repo.soft_delete(service_id=a)
    rows = repo.list_active(project_id=1)
    assert [r.id for r in rows] == [b]


def test_soft_delete_flips_is_active_and_is_idempotent(
    repo: ServicesRepository,
) -> None:
    sid = repo.add(project_id=1, name="alpha", now=_NOW)
    repo.soft_delete(service_id=sid)
    # Second soft_delete of an already-inactive row raises ServiceNotFound.
    with pytest.raises(ServiceNotFound):
        repo.soft_delete(service_id=sid)


def test_soft_delete_unknown_raises_not_found(repo: ServicesRepository) -> None:
    with pytest.raises(ServiceNotFound):
        repo.soft_delete(service_id=999)


def test_soft_delete_then_readd_succeeds(repo: ServicesRepository) -> None:
    """Soft-deleting frees the name for re-add (the unique index covers active
    rows only). This is how the operator recovers from a typo."""
    repo.add(project_id=1, name="alpha", now=_NOW)
    rows = repo.list_active(project_id=1)
    repo.soft_delete(service_id=rows[0].id)
    new_id = repo.add(project_id=1, name="alpha", now=_NOW)
    rows_after = repo.list_active(project_id=1)
    assert [r.id for r in rows_after] == [new_id]


def test_count_active_filters_by_project(repo: ServicesRepository) -> None:
    repo.add(project_id=1, name="alpha", now=_NOW)
    repo.add(project_id=1, name="beta", now=_NOW)
    repo.add(project_id=2, name="gamma", now=_NOW)
    assert repo.count_active(project_id=1) == 2
    assert repo.count_active(project_id=2) == 1
    assert repo.count_active(project_id=99) == 0


def test_count_active_excludes_soft_deleted(repo: ServicesRepository) -> None:
    sid = repo.add(project_id=1, name="alpha", now=_NOW)
    repo.add(project_id=1, name="beta", now=_NOW)
    repo.soft_delete(service_id=sid)
    assert repo.count_active(project_id=1) == 1


def test_list_for_project_returns_all_active(repo: ServicesRepository) -> None:
    """``list_for_project`` is the shape the SalesPersonaAnswerer's
    ``_ServicesRepo`` protocol expects. Returns active rows in deterministic
    order so the catalog answer is stable."""
    a = repo.add(project_id=1, name="alpha", now=_NOW)
    b = repo.add(project_id=1, name="beta", now=_NOW)
    rows = repo.list_for_project(project_id=1)
    assert [r.id for r in rows] == [a, b]


def test_get_by_name_returns_active_match(repo: ServicesRepository) -> None:
    """Case-insensitive lookup used by the concept-ask path."""
    sid = repo.add(project_id=1, name="каньонинг", now=_NOW)
    found = repo.get_by_name(project_id=1, name="Каньонинг")
    assert found is not None
    assert found.id == sid


def test_get_by_name_returns_none_for_inactive(repo: ServicesRepository) -> None:
    sid = repo.add(project_id=1, name="каньонинг", now=_NOW)
    repo.soft_delete(service_id=sid)
    assert repo.get_by_name(project_id=1, name="каньонинг") is None


def test_init_schema_is_idempotent(tmp_path: Path) -> None:
    db = str(tmp_path / "sales.sqlite3")
    a = ServicesRepository(db_path=db)
    a.add(project_id=1, name="alpha", now=_NOW)
    # Re-opening must not wipe rows or error.
    b = ServicesRepository(db_path=db)
    assert [row.name for row in b.list_active(project_id=1)] == ["alpha"]


def test_add_rejects_naive_now(repo: ServicesRepository) -> None:
    naive = datetime(2026, 5, 27, 9, 0)
    with pytest.raises(ValueError):
        repo.add(project_id=1, name="alpha", now=naive)


def test_add_rejects_blank_name(repo: ServicesRepository) -> None:
    """Defence-in-depth: the bot parser already strips, but the repo must
    refuse blank rows so a programmatic caller cannot poison the catalog."""
    with pytest.raises(ValueError):
        repo.add(project_id=1, name="   ", now=_NOW)


def test_find_by_name_case_insensitive(repo: ServicesRepository) -> None:
    """Alias matching the Story 12.01 spec surface — case-insensitive lookup."""
    sid = repo.add(project_id=1, name="каньонинг", now=_NOW)
    found = repo.find_by_name(project_id=1, name="КАНЬОНИНГ")
    assert found is not None
    assert found.id == sid


def test_find_by_name_returns_none_for_unknown(repo: ServicesRepository) -> None:
    assert repo.find_by_name(project_id=1, name="ghost") is None


def test_get_returns_row_by_id(repo: ServicesRepository) -> None:
    sid = repo.add(project_id=1, name="alpha", now=_NOW)
    found = repo.get(service_id=sid)
    assert found is not None
    assert found.id == sid
    assert found.name == "alpha"


def test_get_returns_none_for_unknown_id(repo: ServicesRepository) -> None:
    assert repo.get(service_id=99999) is None


def test_get_returns_soft_deleted_row(repo: ServicesRepository) -> None:
    """``get`` ignores ``is_active`` so callers can introspect audit history."""
    sid = repo.add(project_id=1, name="alpha", now=_NOW)
    repo.soft_delete(service_id=sid)
    found = repo.get(service_id=sid)
    assert found is not None
    assert found.is_active is False
