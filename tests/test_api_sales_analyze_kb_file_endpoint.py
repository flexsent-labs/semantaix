"""``POST /sales/materials/analyze-kb-file`` round-trip + auth.

The endpoint is service-token-gated. The bot_gateway hook calls it from
the KB-upload success branch with the freshly resolved project_id +
operator_file_short_id; the api dispatches into the analyzer and returns
the ``AnalysisOutcome`` shape verbatim.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.main import app as api_app
from services.api.app.sales.client_materials_analyzer import AnalysisOutcome


class _StubAnalyzer:
    def __init__(self, *, outcome: AnalysisOutcome) -> None:
        self._outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def analyze_and_register(
        self, *, project_id: int, operator_file_short_id: str, now: Any
    ) -> AnalysisOutcome:
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
    analyzer = _StubAnalyzer(
        outcome=AnalysisOutcome(
            registered=True, material_id=42, reason="ok"
        )
    )
    monkeypatch.setattr(api_main, "client_materials_analyzer", analyzer)
    return {"analyzer": analyzer, "client": TestClient(api_app)}


def test_returns_outcome_when_authorized(env: dict[str, Any]) -> None:
    response = env["client"].post(
        "/sales/materials/analyze-kb-file",
        json={"project_id": 7, "operator_file_short_id": "ABCDEFGH"},
        headers={"Authorization": "Bearer test-bot-token"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "registered": True,
        "material_id": 42,
        "reason": "ok",
    }
    assert env["analyzer"].calls == [
        {
            "project_id": 7,
            "operator_file_short_id": "ABCDEFGH",
            "now": pytest.approx(env["analyzer"].calls[0]["now"]),
        }
    ]
    # The injected ``now`` must be tz-aware so the analyzer can persist it.
    captured_now = env["analyzer"].calls[0]["now"]
    assert captured_now.tzinfo is not None


def test_endpoint_rejects_missing_bearer(
    env: dict[str, Any],
) -> None:
    response = env["client"].post(
        "/sales/materials/analyze-kb-file",
        json={"project_id": 1, "operator_file_short_id": "X"},
    )
    assert response.status_code == 401


def test_endpoint_rejects_wrong_bearer(env: dict[str, Any]) -> None:
    response = env["client"].post(
        "/sales/materials/analyze-kb-file",
        json={"project_id": 1, "operator_file_short_id": "X"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 401


def test_outcome_not_registered_returned_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api_main.settings, "internal_service_token", "test-bot-token"
    )
    analyzer = _StubAnalyzer(
        outcome=AnalysisOutcome(
            registered=False, material_id=None, reason="confidential_kb_file"
        )
    )
    monkeypatch.setattr(api_main, "client_materials_analyzer", analyzer)
    client = TestClient(api_app)
    response = client.post(
        "/sales/materials/analyze-kb-file",
        json={"project_id": 1, "operator_file_short_id": "HIDE"},
        headers={"Authorization": "Bearer test-bot-token"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "registered": False,
        "material_id": None,
        "reason": "confidential_kb_file",
    }


def test_accepts_explicit_now_for_deterministic_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api_main.settings, "internal_service_token", "test-bot-token"
    )
    analyzer = _StubAnalyzer(
        outcome=AnalysisOutcome(registered=False, material_id=None, reason="x")
    )
    monkeypatch.setattr(api_main, "client_materials_analyzer", analyzer)
    client = TestClient(api_app)
    response = client.post(
        "/sales/materials/analyze-kb-file",
        json={
            "project_id": 1,
            "operator_file_short_id": "X",
            "now": "2026-05-26T12:00:00+00:00",
        },
        headers={"Authorization": "Bearer test-bot-token"},
    )
    assert response.status_code == 200
    captured = analyzer.calls[0]["now"]
    from datetime import UTC, datetime

    assert captured == datetime(2026, 5, 26, 12, 0, tzinfo=UTC)
