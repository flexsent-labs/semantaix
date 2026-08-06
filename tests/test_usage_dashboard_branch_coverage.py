"""Branch coverage for _aggregate_tiles, _series_for_charts, and usage_raw edge cases.

Story 14.06 — fills the coverage gaps for messages/hitl branches and raw-endpoint
error paths (invalid day_utc, unknown tracker_type).
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from services.api.app.usage.migrations import bootstrap_usage_db
from services.api.app.usage.repositories import UsageDailySummaryRow
from services.web_ui.app.main import app


def _client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


def _principal():
    return {"username": "alice", "role": "admin"}


def _db(tmp_path) -> str:
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    return db


def _insert_summary(db: str, *, day: str, tracker: str, **kwargs) -> None:
    defaults = {
        "project_id": 1,
        "model_name": None,
        "prompt_tokens_total": None,
        "completion_tokens_total": None,
        "cost_usd_total": None,
        "wasted_cost_usd": None,
        "call_count": 0,
        "in_count": None,
        "out_count": None,
        "hitl_created_count": None,
        "hitl_assigned_count": None,
        "hitl_replied_count": None,
        "hitl_resolved_count": None,
    }
    defaults.update(kwargs)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO usage_daily_summary"
            " (project_id, day_utc, tracker_type, model_name,"
            "  prompt_tokens_total, completion_tokens_total, cost_usd_total,"
            "  wasted_cost_usd, call_count, in_count, out_count,"
            "  hitl_created_count, hitl_assigned_count,"
            "  hitl_replied_count, hitl_resolved_count)"
            " VALUES (:project_id, :day, :tracker_type, :model_name,"
            "  :prompt_tokens_total, :completion_tokens_total, :cost_usd_total,"
            "  :wasted_cost_usd, :call_count, :in_count, :out_count,"
            "  :hitl_created_count, :hitl_assigned_count,"
            "  :hitl_replied_count, :hitl_resolved_count)",
            {"day": day, "tracker_type": tracker, **defaults},
        )


def test_aggregate_tiles_includes_llm_totals() -> None:
    from services.web_ui.app.usage_dashboard import _aggregate_tiles

    row = UsageDailySummaryRow(
        project_id=1,
        day_utc="2026-05-25",
        tracker_type="llm",
        model_name="test-model",
        prompt_tokens_total=100,
        completion_tokens_total=25,
        cost_usd_total=0.12345,
        wasted_cost_usd=0.01,
        call_count=1,
        in_count=None,
        out_count=None,
        hitl_created_count=None,
        hitl_assigned_count=None,
        hitl_replied_count=None,
        hitl_resolved_count=None,
    )

    assert _aggregate_tiles([row]) == {
        "llm_cost": 0.1235,
        "llm_tokens": 125,
        "llm_wasted": 0.01,
        "msg_count": 0,
        "hitl_count": 0,
    }


# ---------------------------------------------------------------------------
# Dashboard route: messages + hitl branches in _aggregate_tiles / _series_for_charts
# ---------------------------------------------------------------------------

def test_dashboard_messages_and_hitl_branches(tmp_path):
    """Seed messages + hitl summary rows; verifies aggregate + series branches execute."""
    db = _db(tmp_path)
    yesterday = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
    _insert_summary(db, day=yesterday, tracker="messages", call_count=5,
                    in_count=3, out_count=2)
    _insert_summary(db, day=yesterday, tracker="hitl", call_count=2)

    import services.web_ui.app.usage_dashboard as mod

    with (
        patch("services.web_ui.app.usage_dashboard._resolve_principal",
              new=AsyncMock(return_value=_principal())),
        patch.object(mod, "_settings") as ms,
    ):
        ms.usage_db_path = db
        resp = _client().get("/admin/usage?project_id=1&window=1d")

    assert resp.status_code == 200
    assert "Сообщения" in resp.text
    assert "HITL события" in resp.text


def test_dashboard_series_out_of_window_row_skipped(tmp_path):
    """Rows with day_utc outside the queried window are silently skipped."""
    db = _db(tmp_path)
    old_day = (date.today() - timedelta(days=60)).isoformat()
    _insert_summary(db, day=old_day, tracker="llm",
                    cost_usd_total=99.0, call_count=1)

    import services.web_ui.app.usage_dashboard as mod

    with (
        patch("services.web_ui.app.usage_dashboard._resolve_principal",
              new=AsyncMock(return_value=_principal())),
        patch.object(mod, "_settings") as ms,
    ):
        ms.usage_db_path = db
        resp = _client().get("/admin/usage?project_id=1&window=1w")

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# _series_for_charts: out-of-window row is skipped (continue branch)
# ---------------------------------------------------------------------------

def test_series_for_charts_out_of_window_row_skipped():
    """Row whose day_utc falls outside (from_date, to_date) is silently skipped."""
    from services.api.app.usage.repositories import UsageDailySummaryRow
    from services.web_ui.app.usage_dashboard import _series_for_charts

    in_window = date(2026, 5, 25)
    out_of_window = date(2026, 5, 20)
    row_in = UsageDailySummaryRow(
        project_id=1, day_utc=in_window.isoformat(), tracker_type="llm",
        model_name="haiku", prompt_tokens_total=100, completion_tokens_total=50,
        cost_usd_total=0.01, wasted_cost_usd=0.0, call_count=1,
        in_count=None, out_count=None,
        hitl_created_count=None, hitl_assigned_count=None,
        hitl_replied_count=None, hitl_resolved_count=None,
    )
    row_out = UsageDailySummaryRow(
        project_id=1, day_utc=out_of_window.isoformat(), tracker_type="llm",
        model_name="haiku", prompt_tokens_total=100, completion_tokens_total=50,
        cost_usd_total=99.0, wasted_cost_usd=0.0, call_count=1,
        in_count=None, out_count=None,
        hitl_created_count=None, hitl_assigned_count=None,
        hitl_replied_count=None, hitl_resolved_count=None,
    )
    series = _series_for_charts([row_in, row_out], in_window, in_window)
    assert series["llm_cost"] == [0.01]   # out-of-window 99.0 was skipped


# ---------------------------------------------------------------------------
# usage_raw error paths
# ---------------------------------------------------------------------------

def test_usage_raw_invalid_day_utc_returns_400(tmp_path):
    db = _db(tmp_path)

    import services.web_ui.app.usage_dashboard as mod

    with (
        patch("services.web_ui.app.usage_dashboard._resolve_principal",
              new=AsyncMock(return_value=_principal())),
        patch.object(mod, "_settings") as ms,
    ):
        ms.usage_db_path = db
        resp = _client().get(
            "/admin/usage/raw?project_id=1&day_utc=not-a-date&tracker_type=llm"
        )

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid day_utc"


def test_usage_raw_unknown_tracker_type_returns_400(tmp_path):
    db = _db(tmp_path)
    today = date.today()
    day = (today - timedelta(days=2)).isoformat()

    import services.web_ui.app.usage_dashboard as mod

    with (
        patch("services.web_ui.app.usage_dashboard._resolve_principal",
              new=AsyncMock(return_value=_principal())),
        patch.object(mod, "_settings") as ms,
    ):
        ms.usage_db_path = db
        resp = _client().get(
            f"/admin/usage/raw?project_id=1&day_utc={day}&tracker_type=unknown"
        )

    assert resp.status_code == 400
    assert resp.json()["error"] == "unknown tracker_type"
