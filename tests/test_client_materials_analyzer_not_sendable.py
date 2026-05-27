"""LLM rules the file out — no repo write; outcome carries the reason."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.operator_files_view import KbFileMaterialView
from services.api.app.sales.client_materials_analyzer import (
    ClientMaterialsAnalyzer,
)

_NOW = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)


class _FakeOperatorFilesView:
    def __init__(self, *, view: KbFileMaterialView | None) -> None:
        self._view = view

    def get_for_kb_material(self, *, short_id: str) -> KbFileMaterialView | None:
        return self._view


class _StubOpenRouter:
    def __init__(self, *, response: dict[str, Any]) -> None:
        self._response = response
        self.calls = 0

    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> dict[str, Any]:
        self.calls += 1
        return self._response


class _RecordingMaterialsRepo:
    def __init__(self) -> None:
        self.adds: list[dict[str, Any]] = []

    def add(self, **kwargs: Any) -> int:  # pragma: no cover - asserted via empty list
        self.adds.append(kwargs)
        return -1


@pytest.mark.asyncio
async def test_not_sendable_skips_repo_write() -> None:
    view = KbFileMaterialView(
        short_id="ZX",
        mime_type="application/pdf",
        file_extension="pdf",
        byte_size=4096,
        local_path="/data/uploads/invoice.pdf",
        is_confidential=False,
        extracted_text="ИНВОЙС № 12345. Контрагент ООО Ромашка.",
        project_id=2,
    )
    files = _FakeOperatorFilesView(view=view)
    openrouter = _StubOpenRouter(
        response={
            "sendable": False,
            "reason": "internal invoice",
            "suggested_kind": "pdf",
            "suggested_caption": None,
        }
    )
    materials = _RecordingMaterialsRepo()
    analyzer = ClientMaterialsAnalyzer(
        openrouter=openrouter,
        operator_files_view=files,
        materials_repo=materials,
    )

    outcome = await analyzer.analyze_and_register(
        project_id=2, operator_file_short_id="ZX", now=_NOW
    )

    assert outcome.registered is False
    assert outcome.material_id is None
    assert outcome.reason == "internal invoice"
    assert materials.adds == []
    assert openrouter.calls == 1
