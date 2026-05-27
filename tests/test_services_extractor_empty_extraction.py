"""Empty ``services`` array is a valid LLM response (Story 12.05c).

The LLM may judge the file to be non-service-shaped (a personal letter,
an invoice, an internal memo); it returns ``services: []`` with a short
Russian ``reason``. The extractor MUST NOT call ``services_repo.add``;
``ExtractionOutcome`` carries the reason verbatim.
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


class _ExplodingRepo:
    def find_by_name(self, **_kwargs: Any):
        raise AssertionError(
            "find_by_name must not be called when LLM returns empty services"
        )

    def add(self, **_kwargs: Any) -> int:
        raise AssertionError(
            "services_repo.add must not be called when LLM returns []"
        )


@pytest.mark.asyncio
async def test_empty_services_array_skips_all_repo_writes() -> None:
    view = KbFileMaterialView(
        short_id="EMPTY01",
        mime_type="application/pdf",
        file_extension="pdf",
        byte_size=2048,
        local_path="/data/uploads/letter.pdf",
        is_confidential=False,
        extracted_text="Уважаемый Иван Петрович, ...",
        project_id=3,
    )
    openrouter = _StubOpenRouter(
        response={"services": [], "reason": "личное письмо"}
    )
    extractor = ServicesExtractor(
        openrouter=openrouter,
        operator_files_view=_StaticView(view=view),
        services_repo=_ExplodingRepo(),
    )

    outcome = await extractor.extract_and_register(
        project_id=3, operator_file_short_id="EMPTY01", now=_NOW
    )

    assert outcome.added == []
    assert outcome.skipped_existing == []
    assert outcome.reason == "личное письмо"
