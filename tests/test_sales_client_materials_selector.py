"""Unit tests for ``ClientMaterialsSelector`` (Story 12.05).

``pick(*, project_id, intent_tags, purpose)`` always appends the ``purpose``
token to the tags it sends the repository, returns the top-1 row by overlap
count, ties broken by ``updated_at`` desc, and ``None`` on no match.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.api.app.sales.client_materials_repository import ClientMaterial
from services.api.app.sales.client_materials_selector import (
    ClientMaterialsSelector,
)


def _material(
    *,
    material_id: int,
    tags: list[str],
    updated_at: str = "2026-05-26T10:00:00+00:00",
    kind: str = "video",
) -> ClientMaterial:
    return ClientMaterial(
        id=material_id,
        project_id=1,
        kind=kind,
        telegram_file_id=None,
        local_path=f"/data/sales/{material_id}.mp4",
        byte_size=10,
        duration_seconds=None,
        caption=None,
        tags=tags,
        source_operator_file_id=None,
        is_active=True,
        created_at=updated_at,
        updated_at=updated_at,
    )


@dataclass
class _SpyRepo:
    rows: list[ClientMaterial]
    calls: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []

    def pick_by_tags(
        self, *, project_id: int, tags: list[str]
    ) -> list[ClientMaterial]:
        self.calls.append({"project_id": project_id, "tags": list(tags)})
        requested = set(tags)
        ranked: list[tuple[int, int, ClientMaterial]] = []
        for row in self.rows:
            if row.project_id != project_id:
                continue
            overlap = len(requested.intersection(row.tags))
            if overlap == 0:
                continue
            ranked.append((-overlap, row.id, row))
        ranked.sort()
        return [item[2] for item in ranked]


def test_pick_returns_none_when_repo_empty() -> None:
    selector = ClientMaterialsSelector(repo=_SpyRepo(rows=[]))
    assert selector.pick(
        project_id=1, intent_tags=["rafting"], purpose="tour_preview"
    ) is None


def test_pick_returns_none_when_no_overlap() -> None:
    selector = ClientMaterialsSelector(
        repo=_SpyRepo(rows=[_material(material_id=1, tags=["catalog"])])
    )
    assert selector.pick(
        project_id=1, intent_tags=["rafting"], purpose="tour_preview"
    ) is None


def test_pick_always_includes_purpose_in_lookup_tags() -> None:
    repo = _SpyRepo(rows=[])
    selector = ClientMaterialsSelector(repo=repo)
    selector.pick(
        project_id=1,
        intent_tags=["rafting", "summer"],
        purpose="tour_preview",
    )
    assert repo.calls == [
        {
            "project_id": 1,
            "tags": ["rafting", "summer", "tour_preview"],
        }
    ]


def test_pick_returns_top_overlap_row() -> None:
    one_match = _material(material_id=1, tags=["tour_preview"])
    three_match = _material(
        material_id=2, tags=["tour_preview", "rafting", "summer"]
    )
    selector = ClientMaterialsSelector(
        repo=_SpyRepo(rows=[one_match, three_match])
    )
    picked = selector.pick(
        project_id=1,
        intent_tags=["rafting", "summer"],
        purpose="tour_preview",
    )
    assert picked is not None
    assert picked.id == 2


def test_pick_ties_broken_by_updated_at_desc() -> None:
    older = _material(
        material_id=1,
        tags=["tour_preview", "rafting"],
        updated_at="2026-04-01T00:00:00+00:00",
    )
    newer = _material(
        material_id=2,
        tags=["tour_preview", "rafting"],
        updated_at="2026-05-26T10:00:00+00:00",
    )
    selector = ClientMaterialsSelector(repo=_SpyRepo(rows=[older, newer]))
    picked = selector.pick(
        project_id=1, intent_tags=["rafting"], purpose="tour_preview"
    )
    assert picked is not None
    assert picked.id == 2


def test_pick_purpose_is_appended_after_intent_tags() -> None:
    """Order matters for observability — purpose is always at the tail."""
    repo = _SpyRepo(rows=[])
    selector = ClientMaterialsSelector(repo=repo)
    selector.pick(
        project_id=2,
        intent_tags=["any", "thing"],
        purpose="equipment_gallery",
    )
    assert repo.calls[0]["tags"][-1] == "equipment_gallery"
