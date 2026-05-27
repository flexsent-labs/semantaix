"""Long extracted text is truncated to the first 6000 chars (Story 12.05c).

The extractor sends only the head of the document to the LLM — service
catalogs typically describe their offerings in the first few pages, so
6000 chars is enough for the offerings overview. The tail must NOT
appear in the user prompt.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.operator_files_view import KbFileMaterialView
from services.api.app.sales.services_extractor import (
    EXTRACTED_TEXT_CAP,
    ServicesExtractor,
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
    def find_by_name(self, **_kwargs: Any):
        return None

    def add(self, **_kwargs: Any) -> int:
        return 1


def test_extracted_text_cap_value() -> None:
    """The cap is 6000 chars per the 12.05c story (slightly larger than
    12.05b's 4000)."""
    assert EXTRACTED_TEXT_CAP == 6000


@pytest.mark.asyncio
async def test_long_text_is_truncated_to_6000_chars() -> None:
    head = "Каталог:"
    tail_marker = "TAIL_PAST_THE_CAP_MUST_NOT_BE_SENT"
    body = head + ("x" * (EXTRACTED_TEXT_CAP - len(head))) + tail_marker
    assert len(body) > EXTRACTED_TEXT_CAP

    view = KbFileMaterialView(
        short_id="LONGSERVICE",
        mime_type="application/pdf",
        file_extension="pdf",
        byte_size=99999,
        local_path="/data/uploads/long.pdf",
        is_confidential=False,
        extracted_text=body,
        project_id=1,
    )
    openrouter = _CapturingOpenRouter(
        response={"services": [], "reason": "ok"}
    )
    extractor = ServicesExtractor(
        openrouter=openrouter,
        operator_files_view=_StaticView(view=view),
        services_repo=_NoopRepo(),
    )

    await extractor.extract_and_register(
        project_id=1, operator_file_short_id="LONGSERVICE", now=_NOW
    )

    assert len(openrouter.user_prompts) == 1
    sent = openrouter.user_prompts[0]
    assert head in sent
    assert tail_marker not in sent
