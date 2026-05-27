"""Unit tests for ``ClientMaterialsAnalyzer`` — happy path (Story 12.05b).

A sendable file: the analyzer reads metadata from the operator-files view,
calls the LLM JSON-out completion, and registers a ``client_materials``
row via the repository. The returned ``AnalysisOutcome`` mirrors the
endpoint payload that the bot_gateway acknowledgement hook consumes.
"""

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
        self.calls: list[str] = []

    def get_for_kb_material(self, *, short_id: str) -> KbFileMaterialView | None:
        self.calls.append(short_id)
        return self._view


class _CapturingOpenRouter:
    def __init__(self, *, response: dict[str, Any]) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user, "model": model})
        return self._response


class _FakeMaterialsRepo:
    def __init__(self) -> None:
        self.adds: list[dict[str, Any]] = []
        self._next_id = 100

    def add(self, **kwargs: Any) -> int:
        self.adds.append(kwargs)
        material_id = self._next_id
        self._next_id += 1
        return material_id


def _build_view(**overrides: Any) -> KbFileMaterialView:
    defaults: dict[str, Any] = {
        "short_id": "ABCDEFGH",
        "mime_type": "application/pdf",
        "file_extension": "pdf",
        "byte_size": 8192,
        "local_path": "/data/uploads/catalog.pdf",
        "is_confidential": False,
        "extracted_text": "Каталог туров на квадроциклах. Маршрут на Ачишхо…",
        "project_id": 7,
    }
    defaults.update(overrides)
    return KbFileMaterialView(**defaults)


@pytest.mark.asyncio
async def test_sendable_registers_material_and_returns_outcome() -> None:
    view = _build_view()
    files = _FakeOperatorFilesView(view=view)
    openrouter = _CapturingOpenRouter(
        response={
            "sendable": True,
            "reason": "tour catalog with public-facing route descriptions",
            "suggested_kind": "pdf",
            "suggested_caption": "Каталог туров",
        }
    )
    materials = _FakeMaterialsRepo()

    analyzer = ClientMaterialsAnalyzer(
        openrouter=openrouter,
        operator_files_view=files,
        materials_repo=materials,
    )

    outcome = await analyzer.analyze_and_register(
        project_id=7, operator_file_short_id="ABCDEFGH", now=_NOW
    )

    assert outcome.registered is True
    assert outcome.material_id == 100
    assert outcome.reason == "tour catalog with public-facing route descriptions"

    assert files.calls == ["ABCDEFGH"]
    assert len(openrouter.calls) == 1
    assert openrouter.calls[0]["system"]  # non-empty system prompt loaded

    assert materials.adds == [
        {
            "project_id": 7,
            "kind": "pdf",
            "local_path": "/data/uploads/catalog.pdf",
            "byte_size": 8192,
            "caption": "Каталог туров",
            "tags": [],
            "telegram_file_id": None,
            "source_operator_file_id": "ABCDEFGH",
            "now": _NOW,
        }
    ]


@pytest.mark.asyncio
async def test_user_prompt_does_not_leak_full_text_to_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The extracted text MUST NEVER reach a log line — per the story.

    The user prompt may contain the text (it's sent to the LLM), but our
    own log lines must omit it.
    """
    secret = "СУПЕРСЕКРЕТНЫЙ_КАТАЛОГ_БАГГИ_2026"
    view = _build_view(extracted_text=secret + " подробности маршрута")
    files = _FakeOperatorFilesView(view=view)
    openrouter = _CapturingOpenRouter(
        response={
            "sendable": True,
            "reason": "ok",
            "suggested_kind": "pdf",
            "suggested_caption": "Каталог",
        }
    )
    materials = _FakeMaterialsRepo()
    analyzer = ClientMaterialsAnalyzer(
        openrouter=openrouter,
        operator_files_view=files,
        materials_repo=materials,
    )

    with caplog.at_level("INFO"):
        await analyzer.analyze_and_register(
            project_id=7, operator_file_short_id="ABCDEFGH", now=_NOW
        )

    for record in caplog.records:
        assert secret not in record.getMessage()
        for value in record.__dict__.values():
            if isinstance(value, str):
                assert secret not in value


@pytest.mark.asyncio
async def test_sendable_video_extension_maps_to_video_kind() -> None:
    view = _build_view(
        file_extension="mp4",
        local_path="/data/uploads/route.mp4",
        byte_size=10240,
        mime_type="video/mp4",
    )
    files = _FakeOperatorFilesView(view=view)
    openrouter = _CapturingOpenRouter(
        response={
            "sendable": True,
            "reason": "route preview video",
            "suggested_kind": "video",
            "suggested_caption": None,
        }
    )
    materials = _FakeMaterialsRepo()
    analyzer = ClientMaterialsAnalyzer(
        openrouter=openrouter,
        operator_files_view=files,
        materials_repo=materials,
    )

    outcome = await analyzer.analyze_and_register(
        project_id=3, operator_file_short_id="VIDEOID1", now=_NOW
    )

    assert outcome.registered is True
    assert materials.adds[0]["kind"] == "video"
    assert materials.adds[0]["caption"] is None
