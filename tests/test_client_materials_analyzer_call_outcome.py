"""Tests that ClientMaterialsAnalyzer passes project_id to complete_json (Story 14.02).

Verifies the moderation_triggered call_outcome is forwarded via project_id so the
recorder inside OpenRouterClient can tag the usage row correctly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.operator_files_view import KbFileMaterialView
from services.api.app.sales.client_materials_analyzer import (
    ClientMaterialsAnalyzer,
)

_NOW = datetime(2026, 6, 11, 12, 0, tzinfo=UTC)


class _CapturingOpenRouter:
    def __init__(self, *, response: dict[str, Any]) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        project_id: int | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user, "model": model, "project_id": project_id})  # noqa: E501
        return self._response


class _FakeOperatorFilesView:
    def __init__(self, *, view: KbFileMaterialView | None) -> None:
        self._view = view

    def get_for_kb_material(self, *, short_id: str) -> KbFileMaterialView | None:
        return self._view


class _FakeMaterialsRepo:
    def add(self, **kwargs: Any) -> int:
        return 1


def _build_view(**overrides: Any) -> KbFileMaterialView:
    defaults: dict[str, Any] = {
        "short_id": "ABCDEFGH",
        "mime_type": "application/pdf",
        "file_extension": "pdf",
        "byte_size": 1024,
        "extracted_text": "Our premium services include extended warranties and support.",
        "is_confidential": False,
        "project_id": 7,
        "local_path": "/data/brochure.pdf",
    }
    defaults.update(overrides)
    return KbFileMaterialView(**defaults)


def _llm_response() -> dict[str, Any]:
    return {
        "is_sendable": True,
        "kind": "pdf",
        "caption": "Services brochure",
        "tags": ["services"],
        "duration_seconds": None,
        "not_sendable_reason": None,
    }


@pytest.mark.asyncio
async def test_complete_json_receives_project_id():
    """project_id is forwarded to complete_json so the recorder tags the row."""
    openrouter = _CapturingOpenRouter(response=_llm_response())
    analyzer = ClientMaterialsAnalyzer(
        openrouter=openrouter,
        operator_files_view=_FakeOperatorFilesView(view=_build_view()),
        materials_repo=_FakeMaterialsRepo(),
    )
    await analyzer.analyze_and_register(operator_file_short_id="ABCDEFGH", project_id=42, now=_NOW)
    assert len(openrouter.calls) == 1
    assert openrouter.calls[0]["project_id"] == 42


@pytest.mark.asyncio
async def test_complete_json_project_id_different_projects():
    """project_id is forwarded per-call — different callers get different IDs."""
    openrouter = _CapturingOpenRouter(response=_llm_response())
    view = _FakeOperatorFilesView(view=_build_view())
    repo = _FakeMaterialsRepo()
    analyzer = ClientMaterialsAnalyzer(
        openrouter=openrouter,
        operator_files_view=view,
        materials_repo=repo,
    )
    await analyzer.analyze_and_register(operator_file_short_id="ABCDEFGH", project_id=7, now=_NOW)
    await analyzer.analyze_and_register(operator_file_short_id="ABCDEFGH", project_id=99, now=_NOW)
    assert openrouter.calls[0]["project_id"] == 7
    assert openrouter.calls[1]["project_id"] == 99
