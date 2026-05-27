"""Story 12.09 — dev-only follow-up tick endpoint is gated by app_env.

``POST /sales/_dev/tick-followup-now`` exists to fast-forward the
proactive follow-up queue inside the epic-12 signoff script without
manipulating the system clock. It must return 404 in non-dev
environments so a misconfigured production never exposes an
unauthenticated tick endpoint.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.sales.followup_fire_handler import FireOutcome


def test_dev_tick_endpoint_returns_404_in_non_dev_env(monkeypatch) -> None:
    monkeypatch.setattr(api_main.settings, "app_env", "production")
    client = TestClient(api_main.app)
    response = client.post("/sales/_dev/tick-followup-now")
    assert response.status_code == 404


def test_dev_tick_endpoint_returns_200_in_dev_env(monkeypatch) -> None:
    monkeypatch.setattr(api_main.settings, "app_env", "dev")
    client = TestClient(api_main.app)
    response = client.post("/sales/_dev/tick-followup-now")
    assert response.status_code == 200
    body = response.json()
    assert "fired" in body
    assert isinstance(body["fired"], int)


def test_dev_tick_endpoint_fires_due_rows_when_in_dev(monkeypatch) -> None:
    """When dev is active and rows are due, each row is fired and counted."""
    monkeypatch.setattr(api_main.settings, "app_env", "dev")

    class _StubRow:
        id = 11
        chat_id = 42
        project_id = 1
        fire_at = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
        status = "scheduled"
        reason = None
        created_at = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)
        updated_at = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)

    monkeypatch.setattr(
        api_main.sales_followup_repository,
        "due",
        lambda *, now, limit=100: [_StubRow()],
    )

    fire_calls: list[Any] = []

    async def fake_fire(row):
        fire_calls.append(row)
        return FireOutcome(sent=True, fallback_text_used=False, text="ok")

    monkeypatch.setattr(api_main.sales_followup_fire_handler, "fire", fake_fire)

    client = TestClient(api_main.app)
    response = client.post("/sales/_dev/tick-followup-now")
    assert response.status_code == 200
    body = response.json()
    assert body["fired"] == 1
    assert len(fire_calls) == 1


def test_dev_tick_endpoint_skips_unrun_row(monkeypatch) -> None:
    """A row that neither sent nor used fallback must not increment ``fired``."""
    monkeypatch.setattr(api_main.settings, "app_env", "dev")

    class _StubRow:
        id = 12
        chat_id = 43
        project_id = 1
        fire_at = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
        status = "scheduled"
        reason = None
        created_at = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)
        updated_at = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)

    monkeypatch.setattr(
        api_main.sales_followup_repository,
        "due",
        lambda *, now, limit=100: [_StubRow()],
    )

    async def fake_fire(row):
        return FireOutcome(sent=False, fallback_text_used=False, text=None)

    monkeypatch.setattr(api_main.sales_followup_fire_handler, "fire", fake_fire)

    client = TestClient(api_main.app)
    response = client.post("/sales/_dev/tick-followup-now")
    assert response.status_code == 200
    assert response.json()["fired"] == 0
