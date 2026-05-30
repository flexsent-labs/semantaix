"""Story 12.16 — per-service scoping schema storage + resolution."""

from __future__ import annotations

from typing import Any

import pytest

from services.api.app.sales.scoping_schema import (
    TRANSFER_SCHEMA,
    ScopingField,
    ScopingSchema,
)
from services.api.app.sales.scoping_schema_repository import (
    PROJECT_DEFAULT_SERVICE_ID,
    ScopingSchemaRepository,
    resolve_scoping_schema,
)


@pytest.fixture
def repo(tmp_path: Any) -> ScopingSchemaRepository:
    return ScopingSchemaRepository(db_path=str(tmp_path / "schemas.db"))


class _ServicesRepo:
    def __init__(self, ids: list[int]) -> None:
        self._ids = ids

    def list_for_project(self, *, project_id: int) -> list[Any]:
        return [type("S", (), {"id": i, "name": f"s{i}"})() for i in self._ids]


def test_set_and_get_round_trips_schema(repo: ScopingSchemaRepository) -> None:
    schema = ScopingSchema(
        (
            ScopingField("dates", "Когда?", kind="text", required=True),
            ScopingField("topic", "Тема?", kind="text", required=False),
        )
    )
    repo.set_schema(project_id=1, service_id=5, schema=schema, updated_by="@a")
    assert repo.get_schema(project_id=1, service_id=5) == schema


def test_get_missing_returns_none(repo: ScopingSchemaRepository) -> None:
    assert repo.get_schema(project_id=1, service_id=9) is None


def test_resolve_single_service_uses_its_schema(
    repo: ScopingSchemaRepository,
) -> None:
    schema = ScopingSchema((ScopingField("topic", "Тема?"),))
    repo.set_schema(project_id=1, service_id=7, schema=schema, updated_by="@a")
    resolved = resolve_scoping_schema(
        repo, _ServicesRepo([7]), project_id=1, transfer_fallback=TRANSFER_SCHEMA
    )
    assert resolved == schema


def test_resolve_multi_service_uses_project_default(
    repo: ScopingSchemaRepository,
) -> None:
    default = ScopingSchema((ScopingField("dates", "Когда?"),))
    repo.set_schema(
        project_id=1,
        service_id=PROJECT_DEFAULT_SERVICE_ID,
        schema=default,
        updated_by="@a",
    )
    resolved = resolve_scoping_schema(
        repo, _ServicesRepo([7, 8]), project_id=1, transfer_fallback=TRANSFER_SCHEMA
    )
    assert resolved == default


def test_resolve_falls_back_to_transfer(repo: ScopingSchemaRepository) -> None:
    resolved = resolve_scoping_schema(
        repo, _ServicesRepo([]), project_id=1, transfer_fallback=TRANSFER_SCHEMA
    )
    assert resolved == TRANSFER_SCHEMA


def test_resolve_single_service_without_schema_uses_project_default(
    repo: ScopingSchemaRepository,
) -> None:
    default = ScopingSchema((ScopingField("dates", "Когда?"),))
    repo.set_schema(
        project_id=1,
        service_id=PROJECT_DEFAULT_SERVICE_ID,
        schema=default,
        updated_by="@a",
    )
    # Service 7 is the only one but has no schema of its own → project default.
    resolved = resolve_scoping_schema(
        repo, _ServicesRepo([7]), project_id=1, transfer_fallback=TRANSFER_SCHEMA
    )
    assert resolved == default
