"""Confidential KB files MUST NEVER become customer-facing materials.

The analyzer short-circuits before the LLM call when the operator-files
view marks the file as ``is_confidential=True``. The mocked LLM records
zero invocations and a ``sales_kb_material_skipped_confidential`` log
line is emitted (no extracted text in the log).
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
    def add(self, **_kwargs: Any) -> int:
        raise AssertionError("repo must not be written for confidential files")


@pytest.mark.asyncio
async def test_confidential_short_circuits_before_llm_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "STAFF_PAYROLL_SHEET_QA1"
    view = KbFileMaterialView(
        short_id="HIDE1234",
        mime_type="application/pdf",
        file_extension="pdf",
        byte_size=4096,
        local_path="/data/uploads/payroll.pdf",
        is_confidential=True,
        extracted_text=secret,
        project_id=1,
    )
    openrouter = _ExplodingOpenRouter()
    analyzer = ClientMaterialsAnalyzer(
        openrouter=openrouter,
        operator_files_view=_StaticView(view=view),
        materials_repo=_ExplodingRepo(),
    )

    with caplog.at_level("INFO"):
        outcome = await analyzer.analyze_and_register(
            project_id=1, operator_file_short_id="HIDE1234", now=_NOW
        )

    assert outcome.registered is False
    assert outcome.material_id is None
    assert outcome.reason == "confidential_kb_file"
    assert openrouter.calls == 0

    skip_records = [
        r for r in caplog.records
        if r.message == "sales_kb_material_skipped_confidential"
    ]
    assert len(skip_records) == 1
    # The log line must reference the short_id but never the extracted text.
    payload = " ".join(
        str(v) for v in skip_records[0].__dict__.values() if isinstance(v, str)
    )
    assert "HIDE1234" in payload
    assert secret not in payload
