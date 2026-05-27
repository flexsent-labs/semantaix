"""Long extracted text is truncated to the first 4000 chars before the LLM call."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.operator_files_view import KbFileMaterialView
from services.api.app.sales.client_materials_analyzer import (
    EXTRACTED_TEXT_CAP,
    ClientMaterialsAnalyzer,
)

_NOW = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)


class _StaticView:
    def __init__(self, *, view: KbFileMaterialView) -> None:
        self._view = view

    def get_for_kb_material(self, *, short_id: str) -> KbFileMaterialView | None:
        return self._view


class _CapturingOpenRouter:
    def __init__(self, *, response: dict[str, Any]) -> None:
        self._response = response
        self.user_prompts: list[str] = []

    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> dict[str, Any]:
        self.user_prompts.append(user)
        return self._response


class _NoopRepo:
    def add(self, **_kwargs: Any) -> int:
        return 1


@pytest.mark.asyncio
async def test_long_text_is_truncated_to_4000_chars() -> None:
    head = "ABC"
    tail_marker = "TAIL_THAT_MUST_NOT_BE_SENT"
    body = head + ("x" * (EXTRACTED_TEXT_CAP - len(head))) + tail_marker
    assert len(body) > EXTRACTED_TEXT_CAP

    view = KbFileMaterialView(
        short_id="LONGTEXT",
        mime_type="application/pdf",
        file_extension="pdf",
        byte_size=99999,
        local_path="/data/uploads/long.pdf",
        is_confidential=False,
        extracted_text=body,
        project_id=1,
    )
    openrouter = _CapturingOpenRouter(
        response={
            "sendable": False,
            "reason": "internal",
            "suggested_kind": "pdf",
            "suggested_caption": None,
        }
    )
    analyzer = ClientMaterialsAnalyzer(
        openrouter=openrouter,
        operator_files_view=_StaticView(view=view),
        materials_repo=_NoopRepo(),
    )

    await analyzer.analyze_and_register(
        project_id=1, operator_file_short_id="LONGTEXT", now=_NOW
    )

    assert len(openrouter.user_prompts) == 1
    sent = openrouter.user_prompts[0]
    # Head appears, tail-marker (past the cap) must NOT appear.
    assert head in sent
    assert tail_marker not in sent
    # The user prompt also has prefix metadata (extension + size) but the
    # extracted text portion must not exceed the cap.
    # Robust check: the marker stays absent regardless of the surrounding
    # template framing.
