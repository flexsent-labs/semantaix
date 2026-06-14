"""Tests for GET /api/usage/wasted endpoint (Story 14.07)."""
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


def _make_client(tmp_path, *, role: str, username: str = "@admin"):
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
    return TestClient(mini_app), db


def _insert_summary(db: str, *, project_id: int, day_utc: str,
                    tracker_type: str = "llm") -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO usage_daily_summary
               (project_id, day_utc, tracker_type, model_name,
                prompt_tokens_total, completion_tokens_total,
                cost_usd_total, wasted_cost_usd, call_count,
                in_count, out_count,
                hitl_created_count, hitl_assigned_count,
                hitl_replied_count, hitl_resolved_count)
               VALUES (?, ?, ?, 'gpt-4o', 0, 0, 5.0, 1.0, 10, 0, 0, 0, 0, 0, 0)""",
            (project_id, day_utc, tracker_type),
        )


def test_wasted_admin_returns_200(tmp_path):
    client, db = _make_client(tmp_path, role="admin")
    _insert_summary(db, project_id=1, day_utc="2026-06-10")
    resp = client.get("/api/usage/wasted", params={
        "project_id": 1, "from_day_utc": "2026-06-01", "to_day_utc": "2026-06-11",
    })
    assert resp.status_code == 200
    assert len(resp.json()["rows"]) == 1


def test_wasted_admin_empty(tmp_path):
    client, db = _make_client(tmp_path, role="admin")
    resp = client.get("/api/usage/wasted", params={
        "project_id": 1, "from_day_utc": "2026-06-01", "to_day_utc": "2026-06-11",
    })
    assert resp.status_code == 200
    assert resp.json()["rows"] == []


def test_wasted_operator_returns_403(tmp_path):
    client, db = _make_client(tmp_path, role="operator", username="@op")
    resp = client.get("/api/usage/wasted", params={
        "project_id": 1, "from_day_utc": "2026-06-01", "to_day_utc": "2026-06-11",
    })
    assert resp.status_code == 403
    assert resp.json()["detail"] == "admin_only"


def test_wasted_only_llm_rows(tmp_path):
    client, db = _make_client(tmp_path, role="admin")
    _insert_summary(db, project_id=1, day_utc="2026-06-10", tracker_type="messages")
    _insert_summary(db, project_id=1, day_utc="2026-06-10", tracker_type="hitl")
    _insert_summary(db, project_id=1, day_utc="2026-06-10", tracker_type="llm")
    resp = client.get("/api/usage/wasted", params={
        "project_id": 1, "from_day_utc": "2026-06-01", "to_day_utc": "2026-06-11",
    })
    rows = resp.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["tracker_type"] == "llm"
