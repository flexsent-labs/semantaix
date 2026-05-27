"""LLM returns malformed JSON or missing/invalid keys (Story 12.05c).

The outcome carries ``reason='llm_schema_violation'`` and zero ``added``;
the failure is logged but never propagated. A transient LLM error
(timeout, HTTP 5xx) likewise resolves to ``llm_error`` and is logged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.openrouter_client import OpenRouterJsonSchemaViolation
from services.api.app.operator_files_view import KbFileMaterialView
from services.api.app.sales.services_extractor import ServicesExtractor

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


class _StubOpenRouter:
    def __init__(self, *, response: dict[str, Any]) -> None:
        self._response = response

    async def complete_json(self, **_kwargs: Any) -> dict[str, Any]:
        return self._response


class _RecordingRepo:
    def __init__(self) -> None:
        self.adds: list[dict[str, Any]] = []

    def find_by_name(self, **_kwargs: Any) -> None:
        return None

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
    repo = _RecordingRepo()
    extractor = ServicesExtractor(
        openrouter=_RaisingOpenRouter(
            exc=OpenRouterJsonSchemaViolation("non-JSON: hi")
        ),
        operator_files_view=_StaticView(view=_view()),
        services_repo=repo,
    )
    with caplog.at_level("WARNING"):
        outcome = await extractor.extract_and_register(
            project_id=1, operator_file_short_id="SH01", now=_NOW
        )
    assert outcome.added == []
    assert outcome.skipped_existing == []
    assert outcome.reason == "llm_schema_violation"
    assert repo.adds == []
    assert any(
        r.message == "sales_services_schema_violation" for r in caplog.records
    )


@pytest.mark.asyncio
async def test_missing_services_key_is_schema_violation() -> None:
    repo = _RecordingRepo()
    extractor = ServicesExtractor(
        openrouter=_StubOpenRouter(response={"reason": "ok"}),
        operator_files_view=_StaticView(view=_view()),
        services_repo=repo,
    )
    outcome = await extractor.extract_and_register(
        project_id=1, operator_file_short_id="SH01", now=_NOW
    )
    assert outcome.added == []
    assert outcome.reason == "llm_schema_violation"
    assert repo.adds == []


@pytest.mark.asyncio
async def test_services_field_not_a_list_is_schema_violation() -> None:
    repo = _RecordingRepo()
    extractor = ServicesExtractor(
        openrouter=_StubOpenRouter(
            response={"services": "Каньонинг, Медовеевка", "reason": "x"},
        ),
        operator_files_view=_StaticView(view=_view()),
        services_repo=repo,
    )
    outcome = await extractor.extract_and_register(
        project_id=1, operator_file_short_id="SH01", now=_NOW
    )
    assert outcome.reason == "llm_schema_violation"
    assert outcome.added == []


@pytest.mark.asyncio
async def test_non_dict_service_entry_is_skipped() -> None:
    """Defensive: the LLM may return a string in the array; that entry is
    skipped without aborting the whole extraction."""
    repo = _RecordingRepo()
    extractor = ServicesExtractor(
        openrouter=_StubOpenRouter(
            response={
                "services": [
                    "Каньонинг",  # bad shape: string instead of dict
                    {"name": "Ивановский водопад", "description": None},
                ],
                "reason": "ok",
            }
        ),
        operator_files_view=_StaticView(view=_view()),
        services_repo=repo,
    )
    outcome = await extractor.extract_and_register(
        project_id=1, operator_file_short_id="SH01", now=_NOW
    )
    assert [a.name for a in outcome.added] == ["Ивановский водопад"]


@pytest.mark.asyncio
async def test_service_entry_without_name_is_skipped_silently() -> None:
    """An entry missing the ``name`` field is dropped (not a schema violation);
    other valid entries in the same array are still registered.
    """
    repo = _RecordingRepo()
    extractor = ServicesExtractor(
        openrouter=_StubOpenRouter(
            response={
                "services": [
                    {"description": "no name here"},
                    {"name": "Каньонинг", "description": None},
                ],
                "reason": "ok",
            }
        ),
        operator_files_view=_StaticView(view=_view()),
        services_repo=repo,
    )
    outcome = await extractor.extract_and_register(
        project_id=1, operator_file_short_id="SH01", now=_NOW
    )
    assert [a.name for a in outcome.added] == ["Каньонинг"]


@pytest.mark.asyncio
async def test_blank_name_is_skipped() -> None:
    repo = _RecordingRepo()
    extractor = ServicesExtractor(
        openrouter=_StubOpenRouter(
            response={
                "services": [
                    {"name": "  ", "description": "x"},
                    {"name": "Ивановский водопад", "description": None},
                ],
                "reason": "ok",
            }
        ),
        operator_files_view=_StaticView(view=_view()),
        services_repo=repo,
    )
    outcome = await extractor.extract_and_register(
        project_id=1, operator_file_short_id="SH01", now=_NOW
    )
    assert [a.name for a in outcome.added] == ["Ивановский водопад"]


@pytest.mark.asyncio
async def test_transient_llm_error_is_caught_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    extractor = ServicesExtractor(
        openrouter=_RaisingOpenRouter(exc=RuntimeError("upstream 502")),
        operator_files_view=_StaticView(view=_view()),
        services_repo=_RecordingRepo(),
    )
    with caplog.at_level("WARNING"):
        outcome = await extractor.extract_and_register(
            project_id=1, operator_file_short_id="SH01", now=_NOW
        )
    assert outcome.added == []
    assert outcome.reason == "llm_error"
    assert any(
        r.message == "sales_services_extract_failed" for r in caplog.records
    )


@pytest.mark.asyncio
async def test_unknown_short_id_is_not_a_schema_violation() -> None:
    class _NoneView:
        def get_for_kb_material(self, *, short_id: str):
            return None

    class _ExplodingLlm:
        async def complete_json(self, **_kwargs: Any):
            raise AssertionError("LLM must not run on missing files")

    extractor = ServicesExtractor(
        openrouter=_ExplodingLlm(),
        operator_files_view=_NoneView(),
        services_repo=_RecordingRepo(),
    )
    outcome = await extractor.extract_and_register(
        project_id=1, operator_file_short_id="GONE", now=_NOW
    )
    assert outcome.added == []
    assert outcome.reason == "operator_file_not_found"


@pytest.mark.asyncio
async def test_no_extracted_text_short_circuits_before_llm() -> None:
    class _ExplodingLlm:
        async def complete_json(self, **_kwargs: Any):
            raise AssertionError(
                "LLM must not run when there is nothing to analyze"
            )

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
    extractor = ServicesExtractor(
        openrouter=_ExplodingLlm(),
        operator_files_view=_StaticView(view=view),
        services_repo=_RecordingRepo(),
    )
    outcome = await extractor.extract_and_register(
        project_id=1, operator_file_short_id="EMPTYTXT", now=_NOW
    )
    assert outcome.added == []
    assert outcome.reason == "empty_extracted_text"


@pytest.mark.asyncio
async def test_missing_project_id_in_view_short_circuits() -> None:
    """The view's ``project_id`` is unused by the extractor (the api endpoint
    supplies ``project_id`` directly), but the analyzer must still bail out
    cleanly if asked for a file that no longer has a knowledge-candidate row.
    The view returning a ``KbFileMaterialView`` with ``project_id=None`` is
    rare but shouldn't crash — the extractor uses the endpoint-supplied id.
    """
    view = KbFileMaterialView(
        short_id="NOPROJ",
        mime_type="application/pdf",
        file_extension="pdf",
        byte_size=4096,
        local_path="/data/uploads/x.pdf",
        is_confidential=False,
        extracted_text="catalog",
        project_id=None,
    )

    class _StubOk:
        async def complete_json(self, **_kwargs: Any):
            return {"services": [], "reason": "ok"}

    extractor = ServicesExtractor(
        openrouter=_StubOk(),
        operator_files_view=_StaticView(view=view),
        services_repo=_RecordingRepo(),
    )
    outcome = await extractor.extract_and_register(
        project_id=5, operator_file_short_id="NOPROJ", now=_NOW
    )
    assert outcome.added == []
    assert outcome.reason == "ok"
