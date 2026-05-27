"""Happy path for ``ServicesExtractor`` (Story 12.05c).

The LLM returns three services; the extractor calls
``services_repo.add(...)`` three times and the returned
``ExtractionOutcome`` lists all three names in ``added`` with their
service ids. The ``skipped_existing`` list stays empty.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.operator_files_view import KbFileMaterialView
from services.api.app.sales.services_extractor import (
    ServicesExtractor,
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


class _FakeServicesRepo:
    def __init__(self) -> None:
        self.existing: dict[tuple[int, str], int] = {}
        self.adds: list[dict[str, Any]] = []
        self._next_id = 1000

    def find_by_name(
        self, *, project_id: int, name: str
    ) -> dict[str, Any] | None:
        sid = self.existing.get((project_id, name.casefold()))
        if sid is None:
            return None
        return {"id": sid, "name": name}

    def add(
        self,
        *,
        project_id: int,
        name: str,
        description_md: str | None,
        tags: list[str],
        now: datetime,
    ) -> int:
        self.adds.append(
            {
                "project_id": project_id,
                "name": name,
                "description_md": description_md,
                "tags": list(tags),
                "now": now,
            }
        )
        sid = self._next_id
        self._next_id += 1
        self.existing[(project_id, name.casefold())] = sid
        return sid


def _build_view(**overrides: Any) -> KbFileMaterialView:
    defaults: dict[str, Any] = {
        "short_id": "ABCDEFGH",
        "mime_type": "application/pdf",
        "file_extension": "pdf",
        "byte_size": 8192,
        "local_path": "/data/uploads/catalog.pdf",
        "is_confidential": False,
        "extracted_text": (
            "Каталог: Медовеевка Лайт — лёгкий маршрут. "
            "Каньонинг — спуск по водопадам. Ивановский водопад."
        ),
        "project_id": 7,
    }
    defaults.update(overrides)
    return KbFileMaterialView(**defaults)


@pytest.mark.asyncio
async def test_three_services_registered_outcome_lists_all_three() -> None:
    view = _build_view()
    files = _FakeOperatorFilesView(view=view)
    openrouter = _CapturingOpenRouter(
        response={
            "services": [
                {"name": "Медовеевка Лайт", "description": "Лёгкий маршрут на квадроциклах."},
                {"name": "Каньонинг", "description": "Спуск по водопадам."},
                {"name": "Ивановский водопад", "description": None},
            ],
            "reason": "tour catalog",
        }
    )
    services = _FakeServicesRepo()

    extractor = ServicesExtractor(
        openrouter=openrouter,
        operator_files_view=files,
        services_repo=services,
    )

    outcome = await extractor.extract_and_register(
        project_id=7, operator_file_short_id="ABCDEFGH", now=_NOW
    )

    assert outcome.reason == "tour catalog"
    assert outcome.skipped_existing == []
    assert [item.name for item in outcome.added] == [
        "Медовеевка Лайт",
        "Каньонинг",
        "Ивановский водопад",
    ]
    assert [item.service_id for item in outcome.added] == [1000, 1001, 1002]

    assert files.calls == ["ABCDEFGH"]
    assert len(openrouter.calls) == 1
    assert openrouter.calls[0]["system"]  # non-empty system prompt loaded

    assert len(services.adds) == 3
    assert services.adds[0] == {
        "project_id": 7,
        "name": "Медовеевка Лайт",
        "description_md": "Лёгкий маршрут на квадроциклах.",
        "tags": [],
        "now": _NOW,
    }
    assert services.adds[2]["description_md"] is None


@pytest.mark.asyncio
async def test_logs_count_only_no_names_or_descriptions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Log line carries counts but never service names or descriptions."""
    secret_name = "СУПЕРСЕКРЕТНЫЙ_ТУР"
    secret_desc = "Закрытое описание маршрута"
    view = _build_view(
        extracted_text=f"{secret_name}: {secret_desc}",
    )
    openrouter = _CapturingOpenRouter(
        response={
            "services": [
                {"name": secret_name, "description": secret_desc},
            ],
            "reason": "tour catalog",
        }
    )
    services = _FakeServicesRepo()
    extractor = ServicesExtractor(
        openrouter=openrouter,
        operator_files_view=_FakeOperatorFilesView(view=view),
        services_repo=services,
    )

    with caplog.at_level("INFO"):
        await extractor.extract_and_register(
            project_id=7, operator_file_short_id="ABCDEFGH", now=_NOW
        )

    # The structured log record exists and reports the counts.
    extracted_records = [
        r for r in caplog.records
        if r.message == "sales_services_extracted_count"
    ]
    assert len(extracted_records) == 1
    record = extracted_records[0]
    assert getattr(record, "count_added", None) == 1
    assert getattr(record, "count_skipped", None) == 0
    assert getattr(record, "project_id", None) == 7

    # No log record (any field) may contain the name or description text.
    for r in caplog.records:
        for value in r.__dict__.values():
            if isinstance(value, str):
                assert secret_name not in value
                assert secret_desc not in value
