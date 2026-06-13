"""Tests for GET /admin/usage route — Story 14.06.

Covers: auth gate, admin vs operator rendering, window selector,
project_id scoping.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from services.web_ui.app.main import app


def _client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


def _mock_principal(role: str = "admin", username: str = "alice"):
    return {"username": username, "role": role}


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------

def test_usage_dashboard_no_session_redirects_to_login():
    with patch(
        "services.web_ui.app.usage_dashboard._resolve_principal",
        new=AsyncMock(return_value=None),
    ):
        resp = _client().get("/admin/usage", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# Admin session — full dashboard
# ---------------------------------------------------------------------------

def test_usage_dashboard_admin_sees_wasted_tile():
    with patch(
        "services.web_ui.app.usage_dashboard._resolve_principal",
        new=AsyncMock(return_value=_mock_principal(role="admin")),
    ):
        resp = _client().get("/admin/usage?project_id=1")
    assert resp.status_code == 200
    assert "Потрачено впустую" in resp.text


def test_usage_dashboard_admin_sees_time_selector():
    with patch(
        "services.web_ui.app.usage_dashboard._resolve_principal",
        new=AsyncMock(return_value=_mock_principal(role="admin")),
    ):
        resp = _client().get("/admin/usage?project_id=1")
    assert resp.status_code == 200
    assert "1d" in resp.text
    assert "1w" in resp.text
    assert "1m" in resp.text


def test_usage_dashboard_admin_sees_three_tracker_tiles():
    with patch(
        "services.web_ui.app.usage_dashboard._resolve_principal",
        new=AsyncMock(return_value=_mock_principal(role="admin")),
    ):
        resp = _client().get("/admin/usage?project_id=1")
    assert resp.status_code == 200
    assert "LLM" in resp.text
    assert "Сообщения" in resp.text
    assert "HITL" in resp.text


# ---------------------------------------------------------------------------
# Operator session — no cost data
# ---------------------------------------------------------------------------

def test_usage_dashboard_operator_no_wasted_tile():
    with patch(
        "services.web_ui.app.usage_dashboard._resolve_principal",
        new=AsyncMock(return_value=_mock_principal(role="operator", username="bob")),
    ):
        resp = _client().get("/admin/usage?project_id=1")
    assert resp.status_code == 200
    assert "Потрачено впустую" not in resp.text


def test_usage_dashboard_operator_sees_tracker_tiles():
    with patch(
        "services.web_ui.app.usage_dashboard._resolve_principal",
        new=AsyncMock(return_value=_mock_principal(role="operator")),
    ):
        resp = _client().get("/admin/usage?project_id=1")
    assert resp.status_code == 200
    assert "LLM" in resp.text
    assert "Сообщения" in resp.text
    assert "HITL" in resp.text


# ---------------------------------------------------------------------------
# Window selection
# ---------------------------------------------------------------------------

def test_usage_dashboard_window_1w_selected():
    with patch(
        "services.web_ui.app.usage_dashboard._resolve_principal",
        new=AsyncMock(return_value=_mock_principal()),
    ):
        resp = _client().get("/admin/usage?project_id=1&window=1w")
    assert resp.status_code == 200
    # 1w radio should be checked
    assert 'value=\'1w\' checked' in resp.text or "value='1w' checked" in resp.text


def test_usage_dashboard_window_1d_selected():
    with patch(
        "services.web_ui.app.usage_dashboard._resolve_principal",
        new=AsyncMock(return_value=_mock_principal()),
    ):
        resp = _client().get("/admin/usage?project_id=1&window=1d")
    assert resp.status_code == 200
    assert "value='1d' checked" in resp.text or 'value=\'1d\' checked' in resp.text


# ---------------------------------------------------------------------------
# No project_id — placeholder shown
# ---------------------------------------------------------------------------

def test_usage_dashboard_no_project_id_shows_prompt():
    with patch(
        "services.web_ui.app.usage_dashboard._resolve_principal",
        new=AsyncMock(return_value=_mock_principal()),
    ):
        resp = _client().get("/admin/usage")
    assert resp.status_code == 200
    assert "project_id" in resp.text
