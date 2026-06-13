"""Custom range cap tests — Story 14.06."""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from services.web_ui.app.main import app
from services.web_ui.app.usage_dashboard import _parse_custom_dates


def _client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


def _principal():
    return {"username": "alice", "role": "admin"}


# ---------------------------------------------------------------------------
# _parse_custom_dates unit tests
# ---------------------------------------------------------------------------

def test_parse_custom_dates_within_30_days_not_capped():
    today = date(2026, 5, 26)
    f, t, capped = _parse_custom_dates("2026-05-01", "2026-05-25", today)
    assert f == date(2026, 5, 1)
    assert t == date(2026, 5, 25)
    assert capped is False


def test_parse_custom_dates_over_30_days_is_capped():
    today = date(2026, 5, 26)
    f, t, capped = _parse_custom_dates("2026-04-01", "2026-05-25", today)
    assert capped is True
    assert (t - f).days < 30


def test_parse_custom_dates_exactly_30_days_not_capped():
    # from 2026-04-26 to 2026-05-25 inclusive = 30 days (diff = 29) → allowed
    today = date(2026, 5, 26)
    f, t, capped = _parse_custom_dates("2026-04-26", "2026-05-25", today)
    assert capped is False
    assert f == date(2026, 4, 26)
    assert t == date(2026, 5, 25)


def test_parse_custom_dates_31_days_is_capped():
    # from 2026-04-25 to 2026-05-25 inclusive = 31 days (diff = 30) → capped
    today = date(2026, 5, 26)
    f, t, capped = _parse_custom_dates("2026-04-25", "2026-05-25", today)
    assert capped is True
    assert (t - f).days < 30


def test_parse_custom_dates_invalid_falls_back():
    today = date(2026, 5, 26)
    f, t, capped = _parse_custom_dates("not-a-date", "also-not", today)
    assert capped is False
    assert (today - f).days <= 31


# ---------------------------------------------------------------------------
# Route renders notice for capped range
# ---------------------------------------------------------------------------

def test_dashboard_custom_range_over_30_days_shows_notice(tmp_path):
    db = str(tmp_path / "usage.db")
    import services.web_ui.app.usage_dashboard as mod
    from services.api.app.usage.migrations import bootstrap_usage_db
    bootstrap_usage_db(db)

    with (
        patch("services.web_ui.app.usage_dashboard._resolve_principal",
              new=AsyncMock(return_value=_principal())),
        patch.object(mod, "_settings") as ms,
    ):
        ms.usage_db_path = db
        resp = _client().get(
            "/admin/usage?project_id=1&from=2026-01-01&to=2026-05-25"
        )

    assert resp.status_code == 200
    assert "30 дн" in resp.text or "Диапазон ограничен" in resp.text


def test_dashboard_custom_range_within_30_days_no_notice(tmp_path):
    db = str(tmp_path / "usage.db")
    import services.web_ui.app.usage_dashboard as mod
    from services.api.app.usage.migrations import bootstrap_usage_db
    bootstrap_usage_db(db)

    with (
        patch("services.web_ui.app.usage_dashboard._resolve_principal",
              new=AsyncMock(return_value=_principal())),
        patch.object(mod, "_settings") as ms,
    ):
        ms.usage_db_path = db
        resp = _client().get(
            "/admin/usage?project_id=1&from=2026-05-01&to=2026-05-25"
        )

    assert resp.status_code == 200
    assert "Диапазон ограничен" not in resp.text
