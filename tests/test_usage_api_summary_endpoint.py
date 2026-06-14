"""Tests for GET /api/usage/summary endpoint (Story 14.07)."""
from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.api.app.admin_auth import SessionPrincipal
from services.api.app.usage.api_router import wire_usage_api_routes
from services.api.app.usage.migrations import bootstrap_usage_db
from services.api.app.usage.repositories import (
    UsageDailySummaryRepository,
    UsageHitlEventRepository,
    UsageIncidentRepository,
    UsageLlmCallRepository,
    UsageMessageRepository,
)


def _make_client(tmp_path, *, role: str = "admin", username: str = "@admin"):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)

    auth_service = MagicMock()
    auth_service.require_session_or_internal.return_value = SessionPrincipal(
        username=username, role=role
    )
    operator_repo = MagicMock()
    if role == "operator":
        op = MagicMock()
        op.project_id = 1
        operator_repo.find_by_username.return_value = op

    mini_app = FastAPI()
    wire_usage_api_routes(
        mini_app,
        auth_service=auth_service,
        summary_repo=UsageDailySummaryRepository(db_path=db),
        llm_repo=UsageLlmCallRepository(db_path=db),
        message_repo=UsageMessageRepository(db_path=db),
        hitl_repo=UsageHitlEventRepository(db_path=db),
        incident_repo=UsageIncidentRepository(db_path=db),
        operator_repo=operator_repo,
    )
    return TestClient(mini_app), db, operator_repo


def _insert_summary(db: str, *, project_id: int, day_utc: str, tracker_type: str,
                    cost_usd_total: float = 1.0, wasted_cost_usd: float = 0.5) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO usage_daily_summary
               (project_id, day_utc, tracker_type, model_name,
                prompt_tokens_total, completion_tokens_total,
                cost_usd_total, wasted_cost_usd, call_count,
                in_count, out_count,
                hitl_created_count, hitl_assigned_count,
                hitl_replied_count, hitl_resolved_count)
               VALUES (?, ?, ?, '', 0, 0, ?, ?, 0, 0, 0, 0, 0, 0, 0)""",
            (project_id, day_utc, tracker_type, cost_usd_total, wasted_cost_usd),
        )


def test_summary_admin_returns_rows(tmp_path):
    client, db, _ = _make_client(tmp_path, role="admin")
    _insert_summary(db, project_id=1, day_utc="2026-06-10", tracker_type="llm")
    resp = client.get("/api/usage/summary", params={
        "project_id": 1, "from_day_utc": "2026-06-01", "to_day_utc": "2026-06-11",
    })
    assert resp.status_code == 200
    assert len(resp.json()["rows"]) == 1


def test_summary_admin_includes_money(tmp_path):
    client, db, _ = _make_client(tmp_path, role="admin")
    _insert_summary(db, project_id=1, day_utc="2026-06-10", tracker_type="llm",
                    cost_usd_total=2.5, wasted_cost_usd=0.8)
    resp = client.get("/api/usage/summary", params={
        "project_id": 1, "from_day_utc": "2026-06-01", "to_day_utc": "2026-06-11",
    })
    row = resp.json()["rows"][0]
    assert row["cost_usd_total"] == 2.5
    assert row["wasted_cost_usd"] == 0.8


def test_summary_operator_excludes_money(tmp_path):
    client, db, _ = _make_client(tmp_path, role="operator", username="@op")
    _insert_summary(db, project_id=1, day_utc="2026-06-10", tracker_type="llm",
                    cost_usd_total=5.0, wasted_cost_usd=1.0)
    resp = client.get("/api/usage/summary", params={
        "project_id": 1, "from_day_utc": "2026-06-01", "to_day_utc": "2026-06-11",
    })
    assert resp.status_code == 200
    row = resp.json()["rows"][0]
    assert row["cost_usd_total"] is None
    assert row["wasted_cost_usd"] is None


def test_summary_operator_sql_excludes_cost_column(tmp_path, monkeypatch):
    """SQL sent to SQLite must not contain 'cost_usd' for operator queries."""
    captured_sqls: list[str] = []

    # Build client (and bootstrap DB) before patching so migrations run cleanly.
    client, db, _ = _make_client(tmp_path, role="operator", username="@op")

    class CapturingConn(sqlite3.Connection):
        def execute(self, sql, params=()):
            captured_sqls.append(sql)
            return super().execute(sql, params)

    original_connect = sqlite3.connect

    def capturing_connect(*args, **kwargs):
        kwargs["factory"] = CapturingConn
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(
        "services.api.app.usage.repositories.sqlite3.connect", capturing_connect
    )
    client.get("/api/usage/summary", params={
        "project_id": 1, "from_day_utc": "2026-06-01", "to_day_utc": "2026-06-11",
    })
    summary_sqls = [s for s in captured_sqls if "usage_daily_summary" in s
                    and "SELECT" in s.upper()]
    assert summary_sqls, "No SELECT on usage_daily_summary was captured"
    for sql in summary_sqls:
        assert "cost_usd" not in sql, f"cost_usd found in operator SQL: {sql!r}"


def test_summary_operator_wrong_project_returns_403(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)

    auth_service = MagicMock()
    auth_service.require_session_or_internal.return_value = SessionPrincipal(
        username="@op", role="operator"
    )
    operator_repo = MagicMock()
    op = MagicMock()
    op.project_id = 99  # different project
    operator_repo.find_by_username.return_value = op

    mini_app = FastAPI()
    wire_usage_api_routes(
        mini_app,
        auth_service=auth_service,
        summary_repo=UsageDailySummaryRepository(db_path=db),
        llm_repo=UsageLlmCallRepository(db_path=db),
        message_repo=UsageMessageRepository(db_path=db),
        hitl_repo=UsageHitlEventRepository(db_path=db),
        incident_repo=UsageIncidentRepository(db_path=db),
        operator_repo=operator_repo,
    )
    resp = TestClient(mini_app).get("/api/usage/summary", params={
        "project_id": 1, "from_day_utc": "2026-06-01", "to_day_utc": "2026-06-11",
    })
    assert resp.status_code == 403
    assert resp.json()["detail"] == "project_not_allowed"


def test_summary_empty_result(tmp_path):
    client, db, _ = _make_client(tmp_path, role="admin")
    resp = client.get("/api/usage/summary", params={
        "project_id": 1, "from_day_utc": "2026-06-01", "to_day_utc": "2026-06-11",
    })
    assert resp.status_code == 200
    assert resp.json()["rows"] == []


def test_summary_trackers_filter(tmp_path):
    client, db, _ = _make_client(tmp_path, role="admin")
    _insert_summary(db, project_id=1, day_utc="2026-06-10", tracker_type="llm")
    _insert_summary(db, project_id=1, day_utc="2026-06-10", tracker_type="messages")
    resp = client.get("/api/usage/summary", params={
        "project_id": 1, "from_day_utc": "2026-06-01", "to_day_utc": "2026-06-11",
        "trackers": "llm",
    })
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert all(r["tracker_type"] == "llm" for r in rows)
