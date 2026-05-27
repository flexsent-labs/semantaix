"""``POST /sales/services/extract-from-kb-file`` round-trip + auth (12.05c).

Service-token gated. The bot_gateway KB-upload hook calls this endpoint
with the freshly resolved project_id + operator_file_short_id; the api
dispatches into the extractor and returns the ``ExtractionOutcome`` shape
verbatim. The endpoint always returns 200 — failures inside the extractor
resolve to ``added=[]`` + a reason; the bot decides whether to surface
anything.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.main import app as api_app
from services.api.app.sales.services_extractor import (
    AddedService,
    ExtractionOutcome,
)


class _StubExtractor:
    def __init__(self, *, outcome: ExtractionOutcome) -> None:
        self._outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def extract_and_register(
        self,
        *,
        project_id: int,
        operator_file_short_id: str,
        now: Any,
    ) -> ExtractionOutcome:
        self.calls.append(
            {
                "project_id": project_id,
                "operator_file_short_id": operator_file_short_id,
                "now": now,
            }
        )
        return self._outcome


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(
        api_main.settings, "internal_service_token", "test-bot-token"
    )
    extractor = _StubExtractor(
        outcome=ExtractionOutcome(
            added=[
                AddedService(service_id=42, name="Медовеевка Лайт"),
                AddedService(service_id=43, name="Каньонинг"),
            ],
            skipped_existing=["Ивановский водопад"],
            reason="tour catalog",
        )
    )
    monkeypatch.setattr(api_main, "services_extractor", extractor)
    return {"extractor": extractor, "client": TestClient(api_app)}


def test_returns_outcome_when_authorized(env: dict[str, Any]) -> None:
    response = env["client"].post(
        "/sales/services/extract-from-kb-file",
        json={"project_id": 7, "operator_file_short_id": "ABCDEFGH"},
        headers={"Authorization": "Bearer test-bot-token"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "added": [
            {"service_id": 42, "name": "Медовеевка Лайт"},
            {"service_id": 43, "name": "Каньонинг"},
        ],
        "skipped_existing": ["Ивановский водопад"],
        "reason": "tour catalog",
    }
    assert len(env["extractor"].calls) == 1
    captured = env["extractor"].calls[0]
    assert captured["project_id"] == 7
    assert captured["operator_file_short_id"] == "ABCDEFGH"
    assert captured["now"].tzinfo is not None


def test_endpoint_rejects_missing_bearer(env: dict[str, Any]) -> None:
    response = env["client"].post(
        "/sales/services/extract-from-kb-file",
        json={"project_id": 1, "operator_file_short_id": "X"},
    )
    assert response.status_code == 401


def test_endpoint_rejects_wrong_bearer(env: dict[str, Any]) -> None:
    response = env["client"].post(
        "/sales/services/extract-from-kb-file",
        json={"project_id": 1, "operator_file_short_id": "X"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 401


def test_empty_outcome_returned_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api_main.settings, "internal_service_token", "test-bot-token"
    )
    extractor = _StubExtractor(
        outcome=ExtractionOutcome(
            added=[],
            skipped_existing=[],
            reason="confidential_kb_file",
        )
    )
    monkeypatch.setattr(api_main, "services_extractor", extractor)
    client = TestClient(api_app)
    response = client.post(
        "/sales/services/extract-from-kb-file",
        json={"project_id": 1, "operator_file_short_id": "HIDE"},
        headers={"Authorization": "Bearer test-bot-token"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "added": [],
        "skipped_existing": [],
        "reason": "confidential_kb_file",
    }


def test_accepts_explicit_now_for_deterministic_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api_main.settings, "internal_service_token", "test-bot-token"
    )
    extractor = _StubExtractor(
        outcome=ExtractionOutcome(added=[], skipped_existing=[], reason="x")
    )
    monkeypatch.setattr(api_main, "services_extractor", extractor)
    client = TestClient(api_app)
    response = client.post(
        "/sales/services/extract-from-kb-file",
        json={
            "project_id": 1,
            "operator_file_short_id": "X",
            "now": "2026-05-26T12:00:00+00:00",
        },
        headers={"Authorization": "Bearer test-bot-token"},
    )
    assert response.status_code == 200
    captured = extractor.calls[0]["now"]
    from datetime import UTC, datetime

    assert captured == datetime(2026, 5, 26, 12, 0, tzinfo=UTC)
