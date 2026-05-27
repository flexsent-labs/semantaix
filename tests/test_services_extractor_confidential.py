"""Confidential KB files MUST NEVER contribute to the services catalog.

When ``operator_files_view`` marks the file ``is_confidential=True``
the extractor short-circuits BEFORE the LLM call. The mocked LLM
records zero invocations and a ``sales_services_skipped_confidential``
log line is emitted that never carries the extracted text.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.operator_files_view import KbFileMaterialView
from services.api.app.sales.services_extractor import ServicesExtractor

_NOW = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)


class _StaticView:
    def __init__(self, *, view: KbFileMaterialView | None) -> None:
        self._view = view

    def get_for_kb_material(self, *, short_id: str) -> KbFileMaterialView | None:
        return self._view


class _ExplodingOpenRouter:
    def __init__(self) -> None:
        self.calls = 0

    async def complete_json(self, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        raise AssertionError("LLM must not be called for confidential files")


class _ExplodingRepo:
    def find_by_name(self, **_kwargs: Any):
        raise AssertionError("repo must not be touched for confidential files")

    def add(self, **_kwargs: Any) -> int:
        raise AssertionError("repo must not be written for confidential files")


@pytest.mark.asyncio
async def test_confidential_short_circuits_before_llm_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "PRIVATE_TOUR_CATALOG_QA1"
    view = KbFileMaterialView(
        short_id="HIDE5678",
        mime_type="application/pdf",
        file_extension="pdf",
        byte_size=4096,
        local_path="/data/uploads/private.pdf",
        is_confidential=True,
        extracted_text=secret,
        project_id=1,
    )
    openrouter = _ExplodingOpenRouter()
    extractor = ServicesExtractor(
        openrouter=openrouter,
        operator_files_view=_StaticView(view=view),
        services_repo=_ExplodingRepo(),
    )

    with caplog.at_level("INFO"):
        outcome = await extractor.extract_and_register(
            project_id=1, operator_file_short_id="HIDE5678", now=_NOW
        )

    assert outcome.added == []
    assert outcome.skipped_existing == []
    assert outcome.reason == "confidential_kb_file"
    assert openrouter.calls == 0

    skip_records = [
        r for r in caplog.records
        if r.message == "sales_services_skipped_confidential"
    ]
    assert len(skip_records) == 1
    # Log carries the short_id but never the extracted text.
    payload = " ".join(
        str(v) for v in skip_records[0].__dict__.values() if isinstance(v, str)
    )
    assert "HIDE5678" in payload
    assert secret not in payload
