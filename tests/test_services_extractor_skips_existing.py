"""Existing services in the repo are soft-skipped (Story 12.05c).

Pre-seed two services in the fake repo. The LLM returns three names —
two of which already exist. Only the new service gets added; the two
pre-existing names show up in ``skipped_existing``. The extractor MUST
NOT overwrite the operator's manually-crafted description on the
existing rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.operator_files_view import KbFileMaterialView
from services.api.app.sales.services_extractor import ServicesExtractor

_NOW = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)


class _StaticView:
    def __init__(self, *, view: KbFileMaterialView) -> None:
        self._view = view

    def get_for_kb_material(self, *, short_id: str) -> KbFileMaterialView | None:
        return self._view


class _StubOpenRouter:
    def __init__(self, *, response: dict[str, Any]) -> None:
        self._response = response

    async def complete_json(self, **_kwargs: Any) -> dict[str, Any]:
        return self._response


class _SeededServicesRepo:
    def __init__(self, *, seeded: dict[tuple[int, str], int]) -> None:
        self.existing = dict(seeded)
        self.adds: list[dict[str, Any]] = []
        self._next_id = 9000

    def find_by_name(
        self, *, project_id: int, name: str
    ) -> dict[str, Any] | None:
        sid = self.existing.get((project_id, name.casefold()))
        if sid is None:
            return None
        return {"id": sid, "name": name}

    def add(
        self,
        *,
        project_id: int,
        name: str,
        description_md: str | None,
        tags: list[str],
        now: datetime,
    ) -> int:
        self.adds.append(
            {
                "project_id": project_id,
                "name": name,
                "description_md": description_md,
                "tags": list(tags),
                "now": now,
            }
        )
        sid = self._next_id
        self._next_id += 1
        self.existing[(project_id, name.casefold())] = sid
        return sid


@pytest.mark.asyncio
async def test_only_new_service_is_added_existing_two_are_listed_as_skipped() -> None:
    view = KbFileMaterialView(
        short_id="UPLOAD01",
        mime_type="application/pdf",
        file_extension="pdf",
        byte_size=2048,
        local_path="/data/uploads/x.pdf",
        is_confidential=False,
        extracted_text="catalog text",
        project_id=5,
    )
    repo = _SeededServicesRepo(
        seeded={
            (5, "медовеевка лайт"): 100,
            (5, "каньонинг"): 101,
        }
    )
    extractor = ServicesExtractor(
        openrouter=_StubOpenRouter(
            response={
                "services": [
                    {"name": "Медовеевка Лайт", "description": "auto-extracted"},
                    {"name": "Каньонинг", "description": "auto"},
                    {"name": "Ивановский водопад", "description": "Водопад в ущелье."},
                ],
                "reason": "tour catalog",
            }
        ),
        operator_files_view=_StaticView(view=view),
        services_repo=repo,
    )

    outcome = await extractor.extract_and_register(
        project_id=5, operator_file_short_id="UPLOAD01", now=_NOW
    )

    assert len(outcome.added) == 1
    assert outcome.added[0].name == "Ивановский водопад"
    assert outcome.skipped_existing == ["Медовеевка Лайт", "Каньонинг"]
    assert outcome.reason == "tour catalog"

    # Only one add call — the new service.
    assert len(repo.adds) == 1
    assert repo.adds[0]["name"] == "Ивановский водопад"
    assert repo.adds[0]["description_md"] == "Водопад в ущелье."


@pytest.mark.asyncio
async def test_case_insensitive_match_against_existing_uses_find_by_name() -> None:
    """``find_by_name`` is the case-insensitive check; the extractor relies
    on the repo's behavior (not its own lowercasing) so a Cyrillic-cased
    pre-existing row blocks duplicates even when the LLM returns a different
    casing.
    """
    view = KbFileMaterialView(
        short_id="UPLOAD02",
        mime_type="application/pdf",
        file_extension="pdf",
        byte_size=2048,
        local_path="/data/uploads/y.pdf",
        is_confidential=False,
        extracted_text="catalog text",
        project_id=5,
    )
    # Seed lowercased to demonstrate the fake's case-folding match.
    repo = _SeededServicesRepo(seeded={(5, "каньонинг"): 42})
    extractor = ServicesExtractor(
        openrouter=_StubOpenRouter(
            response={
                "services": [
                    {"name": "КАНЬОНИНГ", "description": None},
                ],
                "reason": "ok",
            }
        ),
        operator_files_view=_StaticView(view=view),
        services_repo=repo,
    )

    outcome = await extractor.extract_and_register(
        project_id=5, operator_file_short_id="UPLOAD02", now=_NOW
    )

    assert outcome.added == []
    assert outcome.skipped_existing == ["КАНЬОНИНГ"]
    assert repo.adds == []
