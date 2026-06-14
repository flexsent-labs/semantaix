"""Tests for GET /api/usage/raw endpoint (Story 14.07)."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
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


def _recent_day() -> str:
    return (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")


def _old_day() -> str:
    return (datetime.now(UTC) - timedelta(days=31)).strftime("%Y-%m-%d")


def _insert_llm(db: str, *, project_id: int, day_utc: str, cost_usd: float = 0.01) -> None:
    ts = f"{day_utc}T10:00:00Z"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO usage_llm_calls
               (project_id, model_name, prompt_tokens, completion_tokens,
                cost_usd, call_outcome, trace_id, created_at)
               VALUES (?, 'gpt-4o', 10, 5, ?, 'customer_visible_answer', 'tr-1', ?)""",
            (project_id, cost_usd, ts),
        )


def _insert_message(db: str, *, project_id: int, day_utc: str) -> None:
    ts = f"{day_utc}T10:00:00Z"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO usage_messages
               (project_id, direction, participant_role, trace_id, created_at)
               VALUES (?, 'in', 'customer', NULL, ?)""",
            (project_id, ts),
        )


def _insert_hitl(db: str, *, project_id: int, day_utc: str) -> None:
    ts = f"{day_utc}T10:00:00Z"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO usage_hitl_events
               (project_id, event_type, ticket_id, trace_id, created_at)
               VALUES (?, 'created', 42, NULL, ?)""",
            (project_id, ts),
        )


def test_raw_llm_admin_returns_rows(tmp_path):
    client, db = _make_client(tmp_path, role="admin")
    day = _recent_day()
    _insert_llm(db, project_id=1, day_utc=day)
    resp = client.get("/api/usage/raw", params={
        "project_id": 1, "day_utc": day, "tracker_type": "llm",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["rows"]) == 1
    assert "has_more" in body


def test_raw_admin_llm_includes_cost(tmp_path):
    client, db = _make_client(tmp_path, role="admin")
    day = _recent_day()
    _insert_llm(db, project_id=1, day_utc=day, cost_usd=0.05)
    resp = client.get("/api/usage/raw", params={
        "project_id": 1, "day_utc": day, "tracker_type": "llm",
    })
    assert resp.status_code == 200
    assert resp.json()["rows"][0]["cost_usd"] == pytest.approx(0.05)


def test_raw_operator_llm_excludes_cost(tmp_path):
    client, db = _make_client(tmp_path, role="operator", username="@op")
    day = _recent_day()
    _insert_llm(db, project_id=1, day_utc=day, cost_usd=0.05)
    resp = client.get("/api/usage/raw", params={
        "project_id": 1, "day_utc": day, "tracker_type": "llm",
    })
    assert resp.status_code == 200
    assert resp.json()["rows"][0]["cost_usd"] is None


def test_raw_operator_llm_sql_excludes_cost(tmp_path, monkeypatch):
    """SQL sent to SQLite must not contain 'cost_usd' for operator llm queries."""
    captured_sqls: list[str] = []

    # Build client (and bootstrap DB) before patching so migrations run cleanly.
    client, db = _make_client(tmp_path, role="operator", username="@op")

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
    day = _recent_day()
    client.get("/api/usage/raw", params={
        "project_id": 1, "day_utc": day, "tracker_type": "llm",
    })
    llm_sqls = [s for s in captured_sqls
                if "usage_llm_calls" in s and "SELECT" in s.upper()]
    assert llm_sqls, "No SELECT on usage_llm_calls was captured"
    for sql in llm_sqls:
        assert "cost_usd" not in sql, f"cost_usd found in operator SQL: {sql!r}"


def test_raw_old_day_returns_410(tmp_path):
    client, db = _make_client(tmp_path, role="admin")
    old = _old_day()
    resp = client.get("/api/usage/raw", params={
        "project_id": 1, "day_utc": old, "tracker_type": "llm",
    })
    assert resp.status_code == 410
    assert resp.json()["detail"] == "data_purged"


def test_raw_invalid_day_returns_400(tmp_path):
    client, db = _make_client(tmp_path, role="admin")
    resp = client.get("/api/usage/raw", params={
        "project_id": 1, "day_utc": "not-a-date", "tracker_type": "llm",
    })
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_day_utc"


def test_raw_invalid_tracker_type_returns_400(tmp_path):
    client, db = _make_client(tmp_path, role="admin")
    day = _recent_day()
    resp = client.get("/api/usage/raw", params={
        "project_id": 1, "day_utc": day, "tracker_type": "invalid",
    })
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_tracker_type"


def test_raw_page_size_above_max_returns_422(tmp_path):
    client, db = _make_client(tmp_path, role="admin")
    day = _recent_day()
    resp = client.get("/api/usage/raw", params={
        "project_id": 1, "day_utc": day, "tracker_type": "llm", "page_size": 501,
    })
    assert resp.status_code == 422


def test_raw_page_size_zero_returns_422(tmp_path):
    client, db = _make_client(tmp_path, role="admin")
    day = _recent_day()
    resp = client.get("/api/usage/raw", params={
        "project_id": 1, "day_utc": day, "tracker_type": "llm", "page_size": 0,
    })
    assert resp.status_code == 422


def test_raw_messages_tracker(tmp_path):
    client, db = _make_client(tmp_path, role="admin")
    day = _recent_day()
    _insert_message(db, project_id=1, day_utc=day)
    resp = client.get("/api/usage/raw", params={
        "project_id": 1, "day_utc": day, "tracker_type": "messages",
    })
    assert resp.status_code == 200
    assert len(resp.json()["rows"]) == 1


def test_raw_hitl_tracker(tmp_path):
    client, db = _make_client(tmp_path, role="admin")
    day = _recent_day()
    _insert_hitl(db, project_id=1, day_utc=day)
    resp = client.get("/api/usage/raw", params={
        "project_id": 1, "day_utc": day, "tracker_type": "hitl",
    })
    assert resp.status_code == 200
    assert len(resp.json()["rows"]) == 1


def test_raw_has_more_false_when_partial_page(tmp_path):
    client, db = _make_client(tmp_path, role="admin")
    day = _recent_day()
    _insert_llm(db, project_id=1, day_utc=day)
    resp = client.get("/api/usage/raw", params={
        "project_id": 1, "day_utc": day, "tracker_type": "llm", "page_size": 100,
    })
    assert resp.json()["has_more"] is False


def test_raw_empty_returns_empty_list(tmp_path):
    client, db = _make_client(tmp_path, role="admin")
    day = _recent_day()
    resp = client.get("/api/usage/raw", params={
        "project_id": 1, "day_utc": day, "tracker_type": "llm",
    })
    assert resp.status_code == 200
    assert resp.json()["rows"] == []
