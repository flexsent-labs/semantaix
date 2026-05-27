"""LLM returns malformed JSON or missing required keys — outcome is
``llm_schema_violation`` and the failure is logged (never propagated).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.openrouter_client import OpenRouterJsonSchemaViolation
from services.api.app.operator_files_view import KbFileMaterialView
from services.api.app.sales.client_materials_analyzer import (
    ClientMaterialsAnalyzer,
)

_NOW = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)


class _StaticView:
    def __init__(self, *, view: KbFileMaterialView) -> None:
        self._view = view

    def get_for_kb_material(self, *, short_id: str) -> KbFileMaterialView | None:
        return self._view


class _RaisingOpenRouter:
    def __init__(self, *, exc: BaseException) -> None:
        self._exc = exc

    async def complete_json(self, **_kwargs: Any) -> dict[str, Any]:
        raise self._exc


class _SchemaShapedOpenRouter:
    def __init__(self, *, response: dict[str, Any]) -> None:
        self._response = response

    async def complete_json(self, **_kwargs: Any) -> dict[str, Any]:
        return self._response


class _RecordingRepo:
    def __init__(self) -> None:
        self.adds: list[dict[str, Any]] = []

    def add(self, **kwargs: Any) -> int:
        self.adds.append(kwargs)
        return -1


def _view() -> KbFileMaterialView:
    return KbFileMaterialView(
        short_id="SH01",
        mime_type="application/pdf",
        file_extension="pdf",
        byte_size=4096,
        local_path="/data/uploads/x.pdf",
        is_confidential=False,
        extracted_text="hello world",
        project_id=1,
    )


@pytest.mark.asyncio
async def test_non_json_response_returns_schema_violation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    analyzer = ClientMaterialsAnalyzer(
        openrouter=_RaisingOpenRouter(
            exc=OpenRouterJsonSchemaViolation("non-JSON response: hi")
        ),
        operator_files_view=_StaticView(view=_view()),
        materials_repo=_RecordingRepo(),
    )
    with caplog.at_level("WARNING"):
        outcome = await analyzer.analyze_and_register(
            project_id=1, operator_file_short_id="SH01", now=_NOW
        )
    assert outcome.registered is False
    assert outcome.material_id is None
    assert outcome.reason == "llm_schema_violation"
    assert any(
        r.message == "sales_kb_material_schema_violation"
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_missing_required_key_is_schema_violation() -> None:
    """If the LLM omits ``sendable``/``suggested_kind`` the outcome is
    ``llm_schema_violation`` even though the response is valid JSON."""
    analyzer = ClientMaterialsAnalyzer(
        openrouter=_SchemaShapedOpenRouter(
            response={"reason": "ok"},  # missing sendable + suggested_kind
        ),
        operator_files_view=_StaticView(view=_view()),
        materials_repo=_RecordingRepo(),
    )
    outcome = await analyzer.analyze_and_register(
        project_id=1, operator_file_short_id="SH01", now=_NOW
    )
    assert outcome.registered is False
    assert outcome.reason == "llm_schema_violation"


@pytest.mark.asyncio
async def test_unknown_suggested_kind_is_schema_violation() -> None:
    analyzer = ClientMaterialsAnalyzer(
        openrouter=_SchemaShapedOpenRouter(
            response={
                "sendable": True,
                "reason": "ok",
                "suggested_kind": "audio",  # not in {video,photo,pdf,document}
                "suggested_caption": "x",
            },
        ),
        operator_files_view=_StaticView(view=_view()),
        materials_repo=_RecordingRepo(),
    )
    outcome = await analyzer.analyze_and_register(
        project_id=1, operator_file_short_id="SH01", now=_NOW
    )
    assert outcome.registered is False
    assert outcome.reason == "llm_schema_violation"


@pytest.mark.asyncio
async def test_unknown_short_id_is_not_a_schema_violation() -> None:
    """A missing operator file resolves to a specific outcome reason; the
    LLM is never invoked (so this also doubles as a defense-in-depth
    assertion that the analyzer doesn't dispatch on stale ids).
    """

    class _NoneView:
        def get_for_kb_material(self, *, short_id: str):
            return None

    class _ExplodingLlm:
        async def complete_json(self, **_kwargs: Any):
            raise AssertionError("LLM must not run on missing files")

    analyzer = ClientMaterialsAnalyzer(
        openrouter=_ExplodingLlm(),
        operator_files_view=_NoneView(),
        materials_repo=_RecordingRepo(),
    )
    outcome = await analyzer.analyze_and_register(
        project_id=1, operator_file_short_id="GONE", now=_NOW
    )
    assert outcome.registered is False
    assert outcome.reason == "operator_file_not_found"


@pytest.mark.asyncio
async def test_no_extracted_text_short_circuits_before_llm() -> None:
    class _ExplodingLlm:
        async def complete_json(self, **_kwargs: Any):
            raise AssertionError("LLM must not run when there is nothing to analyze")

    view = KbFileMaterialView(
        short_id="EMPTYTXT",
        mime_type="application/pdf",
        file_extension="pdf",
        byte_size=4096,
        local_path="/data/uploads/x.pdf",
        is_confidential=False,
        extracted_text=None,
        project_id=1,
    )
    analyzer = ClientMaterialsAnalyzer(
        openrouter=_ExplodingLlm(),
        operator_files_view=_StaticView(view=view),
        materials_repo=_RecordingRepo(),
    )
    outcome = await analyzer.analyze_and_register(
        project_id=1, operator_file_short_id="EMPTYTXT", now=_NOW
    )
    assert outcome.registered is False
    assert outcome.reason == "empty_extracted_text"


@pytest.mark.asyncio
async def test_no_local_path_short_circuits_before_llm() -> None:
    """``client_materials.local_path`` is NOT NULL — refuse to register
    when the underlying KB file never landed on disk (download failed,
    inline_text upload, etc.)."""

    class _ExplodingLlm:
        async def complete_json(self, **_kwargs: Any):
            raise AssertionError("LLM must not run with no local_path")

    view = KbFileMaterialView(
        short_id="NOPATH01",
        mime_type=None,
        file_extension="txt",
        byte_size=0,
        local_path=None,
        is_confidential=False,
        extracted_text="some text",
        project_id=1,
    )
    analyzer = ClientMaterialsAnalyzer(
        openrouter=_ExplodingLlm(),
        operator_files_view=_StaticView(view=view),
        materials_repo=_RecordingRepo(),
    )
    outcome = await analyzer.analyze_and_register(
        project_id=1, operator_file_short_id="NOPATH01", now=_NOW
    )
    assert outcome.registered is False
    assert outcome.reason == "no_local_path"


@pytest.mark.asyncio
async def test_unexpected_llm_exception_is_caught(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Transient errors (timeout, HTTP 5xx) must NEVER propagate — the
    bot_gateway hook needs the KB ingest to succeed regardless.
    """
    analyzer = ClientMaterialsAnalyzer(
        openrouter=_RaisingOpenRouter(exc=RuntimeError("upstream 502")),
        operator_files_view=_StaticView(view=_view()),
        materials_repo=_RecordingRepo(),
    )
    with caplog.at_level("WARNING"):
        outcome = await analyzer.analyze_and_register(
            project_id=1, operator_file_short_id="SH01", now=_NOW
        )
    assert outcome.registered is False
    assert outcome.reason == "llm_error"
    assert any(
        r.message == "sales_kb_material_llm_error" for r in caplog.records
    )
