"""Drill-down panel tests — Story 14.06."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from services.api.app.usage.migrations import bootstrap_usage_db
from services.web_ui.app.main import app


def _client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


def _principal():
    return {"username": "alice", "role": "admin"}


def _db(tmp_path: Path) -> str:
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    return db


def _insert_llm(db: str, *, day: str) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO usage_llm_calls"
            " (project_id, model_name, prompt_tokens, completion_tokens,"
            "  cost_usd, call_outcome, trace_id, created_at)"
            " VALUES (1, 'haiku', 100, 50, 0.01, 'customer_visible_answer', NULL, ?)",
            (f"{day}T12:00:00Z",),
        )


def test_drill_down_returns_rows_for_valid_day(tmp_path):
    db = _db(tmp_path)
    today = date.today()
    day = (today - timedelta(days=2)).isoformat()
    _insert_llm(db, day=day)

    import services.web_ui.app.usage_dashboard as mod

    with (
        patch("services.web_ui.app.usage_dashboard._resolve_principal",
              new=AsyncMock(return_value=_principal())),
        patch.object(mod, "_settings") as ms,
    ):
        ms.usage_db_path = db
        resp = _client().get(
            f"/admin/usage/raw?project_id=1&day_utc={day}&tracker_type=llm"
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["unavailable"] is False
    assert len(data["rows"]) == 1
    assert data["rows"][0]["model_name"] == "haiku"


def test_drill_down_empty_day_returns_empty_rows(tmp_path):
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
            f"/admin/usage/raw?project_id=1&day_utc={day}&tracker_type=llm"
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["rows"] == []
    assert data["has_more"] is False


def test_drill_down_old_day_returns_410(tmp_path):
    db = _db(tmp_path)
    today = date.today()
    old_day = (today - timedelta(days=35)).isoformat()

    import services.web_ui.app.usage_dashboard as mod

    with (
        patch("services.web_ui.app.usage_dashboard._resolve_principal",
              new=AsyncMock(return_value=_principal())),
        patch.object(mod, "_settings") as ms,
    ):
        ms.usage_db_path = db
        resp = _client().get(
            f"/admin/usage/raw?project_id=1&day_utc={old_day}&tracker_type=llm"
        )

    assert resp.status_code == 410
    data = resp.json()
    assert data["unavailable"] is True
    assert "30-day retention" in data["message"]


def test_drill_down_unauthenticated_returns_401(tmp_path):
    db = _db(tmp_path)
    today = date.today()
    day = (today - timedelta(days=2)).isoformat()

    import services.web_ui.app.usage_dashboard as mod

    with (
        patch("services.web_ui.app.usage_dashboard._resolve_principal",
              new=AsyncMock(return_value=None)),
        patch.object(mod, "_settings") as ms,
    ):
        ms.usage_db_path = db
        resp = _client().get(
            f"/admin/usage/raw?project_id=1&day_utc={day}&tracker_type=llm"
        )

    assert resp.status_code == 401


def test_drill_down_pagination(tmp_path):
    db = _db(tmp_path)
    today = date.today()
    day = (today - timedelta(days=2)).isoformat()
    for _ in range(5):
        _insert_llm(db, day=day)

    import services.web_ui.app.usage_dashboard as mod

    with (
        patch("services.web_ui.app.usage_dashboard._resolve_principal",
              new=AsyncMock(return_value=_principal())),
        patch.object(mod, "_settings") as ms,
    ):
        ms.usage_db_path = db
        page1 = _client().get(
            f"/admin/usage/raw?project_id=1&day_utc={day}&tracker_type=llm&page=1&page_size=3"
        ).json()
        page2 = _client().get(
            f"/admin/usage/raw?project_id=1&day_utc={day}&tracker_type=llm&page=2&page_size=3"
        ).json()

    assert len(page1["rows"]) == 3
    assert page1["has_more"] is True
    assert len(page2["rows"]) == 2
    assert page2["has_more"] is False


def test_drill_down_messages_tracker(tmp_path):
    db = _db(tmp_path)
    today = date.today()
    day = (today - timedelta(days=2)).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO usage_messages"
            " (project_id, direction, participant_role, trace_id, created_at)"
            f" VALUES (1, 'in', 'customer', NULL, '{day}T12:00:00Z')"
        )

    import services.web_ui.app.usage_dashboard as mod

    with (
        patch("services.web_ui.app.usage_dashboard._resolve_principal",
              new=AsyncMock(return_value=_principal())),
        patch.object(mod, "_settings") as ms,
    ):
        ms.usage_db_path = db
        resp = _client().get(
            f"/admin/usage/raw?project_id=1&day_utc={day}&tracker_type=messages"
        )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["rows"]) == 1
    assert data["rows"][0]["direction"] == "in"


def test_drill_down_llm_admin_includes_cost(tmp_path):
    db = _db(tmp_path)
    today = date.today()
    day = (today - timedelta(days=2)).isoformat()
    _insert_llm(db, day=day)

    import services.web_ui.app.usage_dashboard as mod

    with (
        patch("services.web_ui.app.usage_dashboard._resolve_principal",
              new=AsyncMock(return_value={"username": "admin", "role": "admin"})),
        patch.object(mod, "_settings") as ms,
    ):
        ms.usage_db_path = db
        ms.usage_raw_retention_days = 30
        resp = _client().get(
            f"/admin/usage/raw?project_id=1&day_utc={day}&tracker_type=llm"
        )

    assert resp.status_code == 200
    row = resp.json()["rows"][0]
    assert row["cost_usd"] is not None


def test_drill_down_llm_operator_excludes_cost(tmp_path):
    db = _db(tmp_path)
    today = date.today()
    day = (today - timedelta(days=2)).isoformat()
    _insert_llm(db, day=day)

    import services.web_ui.app.usage_dashboard as mod

    with (
        patch("services.web_ui.app.usage_dashboard._resolve_principal",
              new=AsyncMock(return_value={"username": "op", "role": "operator"})),
        patch.object(mod, "_settings") as ms,
    ):
        ms.usage_db_path = db
        ms.usage_raw_retention_days = 30
        resp = _client().get(
            f"/admin/usage/raw?project_id=1&day_utc={day}&tracker_type=llm"
        )

    assert resp.status_code == 200
    row = resp.json()["rows"][0]
    assert row["cost_usd"] is None


def test_drill_down_hitl_tracker(tmp_path):
    db = _db(tmp_path)
    today = date.today()
    day = (today - timedelta(days=2)).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO usage_hitl_events"
            " (project_id, event_type, ticket_id, trace_id, created_at)"
            f" VALUES (1, 'created', 1, NULL, '{day}T12:00:00Z')"
        )

    import services.web_ui.app.usage_dashboard as mod

    with (
        patch("services.web_ui.app.usage_dashboard._resolve_principal",
              new=AsyncMock(return_value=_principal())),
        patch.object(mod, "_settings") as ms,
    ):
        ms.usage_db_path = db
        resp = _client().get(
            f"/admin/usage/raw?project_id=1&day_utc={day}&tracker_type=hitl"
        )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["rows"]) == 1
    assert data["rows"][0]["event_type"] == "created"
