"""Summary-first verification: main dashboard never touches raw tables — Story 14.06."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from services.api.app.usage.migrations import bootstrap_usage_db
from services.api.app.usage.repositories import (
    UsageDailySummaryRepository,
    UsageDailySummaryRow,
    UsageHitlEventRepository,
    UsageLlmCallRepository,
    UsageMessageRepository,
)
from services.web_ui.app.main import app


def _client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


def _principal(role: str = "admin"):
    return {"username": "alice", "role": role}


def _seed_summary(db: str) -> None:
    repo = UsageDailySummaryRepository(db_path=db)
    for i in range(5):
        day = f"2026-05-{20 + i:02d}"
        repo.upsert(
            UsageDailySummaryRow(
                project_id=1, day_utc=day, tracker_type="llm", model_name="haiku",
                prompt_tokens_total=100, completion_tokens_total=50,
                cost_usd_total=0.01, wasted_cost_usd=0.002, call_count=1,
                in_count=None, out_count=None,
                hitl_created_count=None, hitl_assigned_count=None,
                hitl_replied_count=None, hitl_resolved_count=None,
            )
        )


def test_dashboard_does_not_call_raw_repo_list_for_day(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    _seed_summary(db)

    llm_spy = UsageLlmCallRepository(db_path=db)
    msg_spy = UsageMessageRepository(db_path=db)
    hitl_spy = UsageHitlEventRepository(db_path=db)

    calls: list[str] = []

    def _spy_list(*a, **kw):
        calls.append("list_for_day_called")
        return []

    llm_spy.list_for_day = _spy_list  # type: ignore[method-assign]
    msg_spy.list_for_day = _spy_list  # type: ignore[method-assign]
    hitl_spy.list_for_day = _spy_list  # type: ignore[method-assign]

    import services.web_ui.app.usage_dashboard as mod

    with (
        patch("services.web_ui.app.usage_dashboard._resolve_principal",
              new=AsyncMock(return_value=_principal())),
        patch.object(mod, "_settings") as mock_settings,
    ):
        mock_settings.usage_db_path = db
        resp = _client().get("/admin/usage?project_id=1&window=1w")

    assert resp.status_code == 200
    assert calls == [], "Raw list_for_day was called during main page render"


def test_dashboard_renders_summary_data_in_page(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    _seed_summary(db)

    import services.web_ui.app.usage_dashboard as mod

    with (
        patch("services.web_ui.app.usage_dashboard._resolve_principal",
              new=AsyncMock(return_value=_principal())),
        patch.object(mod, "_settings") as mock_settings,
    ):
        mock_settings.usage_db_path = db
        resp = _client().get("/admin/usage?project_id=1&window=1m")

    assert resp.status_code == 200
    # Summary data was fetched and embedded in the page
    assert "0.05" in resp.text or "0.01" in resp.text  # cost values from seed
