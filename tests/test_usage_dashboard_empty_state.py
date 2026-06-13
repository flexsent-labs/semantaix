"""Empty-state and degraded-state rendering — Story 14.06."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from services.web_ui.app.main import app


def _client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


def _principal(role: str = "admin"):
    return {"username": "alice", "role": role}


def test_empty_state_shows_no_data_placeholder(tmp_path):
    db = str(tmp_path / "usage.db")
    # Don't bootstrap — no DB means empty
    import services.web_ui.app.usage_dashboard as mod
    from services.api.app.usage.migrations import bootstrap_usage_db
    bootstrap_usage_db(db)

    with (
        patch("services.web_ui.app.usage_dashboard._resolve_principal",
              new=AsyncMock(return_value=_principal())),
        patch.object(mod, "_settings") as ms,
    ):
        ms.usage_db_path = db
        resp = _client().get("/admin/usage?project_id=1&window=1w")

    assert resp.status_code == 200
    assert "Нет данных" in resp.text or "нет данных" in resp.text.lower()


def test_empty_state_admin_still_sees_wasted_tile_placeholder(tmp_path):
    db = str(tmp_path / "usage.db")
    import services.web_ui.app.usage_dashboard as mod
    from services.api.app.usage.migrations import bootstrap_usage_db
    bootstrap_usage_db(db)

    with (
        patch("services.web_ui.app.usage_dashboard._resolve_principal",
              new=AsyncMock(return_value=_principal(role="admin"))),
        patch.object(mod, "_settings") as ms,
    ):
        ms.usage_db_path = db
        resp = _client().get("/admin/usage?project_id=1")

    assert resp.status_code == 200
    assert "Потрачено впустую" in resp.text


def test_empty_state_operator_no_wasted_tile(tmp_path):
    db = str(tmp_path / "usage.db")
    import services.web_ui.app.usage_dashboard as mod
    from services.api.app.usage.migrations import bootstrap_usage_db
    bootstrap_usage_db(db)

    with (
        patch("services.web_ui.app.usage_dashboard._resolve_principal",
              new=AsyncMock(return_value=_principal(role="operator"))),
        patch.object(mod, "_settings") as ms,
    ):
        ms.usage_db_path = db
        resp = _client().get("/admin/usage?project_id=1")

    assert resp.status_code == 200
    assert "Потрачено впустую" not in resp.text
