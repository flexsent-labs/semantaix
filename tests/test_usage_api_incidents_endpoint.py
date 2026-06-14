"""Tests for GET /api/usage/incidents endpoint (Story 14.07)."""
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
    return TestClient(mini_app), db


def _insert_incident(db: str, *, project_id: int, started_at: str,
                     ended_at: str | None = None) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO usage_incidents
               (project_id, started_at, ended_at, breached_trackers,
                peak_pct, total_excess_cost_usd)
               VALUES (?, ?, ?, '["llm"]', NULL, NULL)""",
            (project_id, started_at, ended_at),
        )


def test_incidents_admin_empty(tmp_path):
    client, db = _make_client(tmp_path, role="admin")
    resp = client.get("/api/usage/incidents", params={
        "project_id": 1, "from_ts": "2026-06-01T00:00:00Z",
        "to_ts": "2026-06-11T00:00:00Z",
    })
    assert resp.status_code == 200
    assert resp.json()["incidents"] == []


def test_incidents_admin_returns_incidents(tmp_path):
    client, db = _make_client(tmp_path, role="admin")
    _insert_incident(db, project_id=1, started_at="2026-06-05T10:00:00Z",
                     ended_at="2026-06-05T11:00:00Z")
    resp = client.get("/api/usage/incidents", params={
        "project_id": 1, "from_ts": "2026-06-01T00:00:00Z",
        "to_ts": "2026-06-11T00:00:00Z",
    })
    assert resp.status_code == 200
    assert len(resp.json()["incidents"]) == 1


def test_incidents_operator_can_access_own_project(tmp_path):
    client, db = _make_client(tmp_path, role="operator", username="@op")
    _insert_incident(db, project_id=1, started_at="2026-06-05T10:00:00Z")
    resp = client.get("/api/usage/incidents", params={
        "project_id": 1, "from_ts": "2026-06-01T00:00:00Z",
        "to_ts": "2026-06-11T00:00:00Z",
    })
    assert resp.status_code == 200
    assert len(resp.json()["incidents"]) == 1


def test_incidents_operator_wrong_project_returns_403(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)

    auth_service = MagicMock()
    auth_service.require_session_or_internal.return_value = SessionPrincipal(
        username="@op", role="operator"
    )
    operator_repo = MagicMock()
    op = MagicMock()
    op.project_id = 99
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
    resp = TestClient(mini_app).get("/api/usage/incidents", params={
        "project_id": 1, "from_ts": "2026-06-01T00:00:00Z",
        "to_ts": "2026-06-11T00:00:00Z",
    })
    assert resp.status_code == 403
    assert resp.json()["detail"] == "project_not_allowed"


def test_incidents_response_shape(tmp_path):
    client, db = _make_client(tmp_path, role="admin")
    _insert_incident(db, project_id=1, started_at="2026-06-05T10:00:00Z",
                     ended_at="2026-06-05T11:00:00Z")
    resp = client.get("/api/usage/incidents", params={
        "project_id": 1, "from_ts": "2026-06-01T00:00:00Z",
        "to_ts": "2026-06-11T00:00:00Z",
    })
    inc = resp.json()["incidents"][0]
    assert "id" in inc
    assert "project_id" in inc
    assert "started_at" in inc
    assert "ended_at" in inc
    assert "breached_trackers" in inc
