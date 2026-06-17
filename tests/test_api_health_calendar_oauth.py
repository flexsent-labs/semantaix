"""/health/calendar-oauth endpoint + production redirect guard."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

import services.api.app.main as main_mod
from services.api.app.main import app as api_app


def test_health_calendar_oauth_ok_when_prod_ready(monkeypatch):
    monkeypatch.setattr(
        main_mod,
        "_calendar_oauth_production_status",
        lambda: (
            True,
            {
                "prod_ready": True,
                "redirect_uri": (
                    "https://semantaix.flexsentlabs.com/api/calendar/oauth/callback"
                ),
            },
        ),
    )
    resp = TestClient(api_app).get("/health/calendar-oauth")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["prod_ready"] is True


def test_health_calendar_oauth_503_when_misconfigured(monkeypatch):
    monkeypatch.setattr(
        main_mod,
        "_calendar_oauth_production_status",
        lambda: (
            False,
            {
                "prod_ready": False,
                "reason": "dev_redirect_in_production",
                "redirect_uri": "https://foo.ngrok-free.dev/api/calendar/oauth/callback",
            },
        ),
    )
    resp = TestClient(api_app).get("/health/calendar-oauth")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False
    assert body["reason"] == "dev_redirect_in_production"


@pytest.mark.asyncio
async def test_startup_alerts_when_calendar_oauth_misconfigured(monkeypatch, caplog):
    monkeypatch.setattr(main_mod.settings, "app_env", "production")
    monkeypatch.setattr(
        main_mod,
        "_calendar_oauth_production_status",
        lambda: (
            False,
            {
                "prod_ready": False,
                "reason": "redirect_host_mismatch",
            },
        ),
    )

    alerts: list[tuple] = []

    async def _spy(*, fingerprint, severity, summary):
        alerts.append((fingerprint, severity, summary))
        return (1, True, "sent")

    monkeypatch.setattr(main_mod, "_record_and_alert_incident", _spy)
    with caplog.at_level(logging.ERROR):
        await main_mod.validate_calendar_oauth_production_on_startup()

    assert any(
        r.getMessage() == "calendar_oauth_production_misconfigured" for r in caplog.records
    )
    assert len(alerts) == 1
    fingerprint, severity, summary = alerts[0]
    assert fingerprint == "calendar_oauth_production_misconfigured"
    assert severity == "critical"
    assert "redirect_host_mismatch" in summary


@pytest.mark.asyncio
async def test_startup_logs_ready_when_prod_configured(monkeypatch, caplog):
    monkeypatch.setattr(
        main_mod,
        "_calendar_oauth_production_status",
        lambda: (
            True,
            {
                "prod_ready": True,
                "redirect_uri": (
                    "https://semantaix.flexsentlabs.com/api/calendar/oauth/callback"
                ),
            },
        ),
    )

    alerts: list[tuple] = []

    async def _spy(*, fingerprint, severity, summary):
        alerts.append((fingerprint, severity, summary))
        return (1, False, "not_critical")

    monkeypatch.setattr(main_mod, "_record_and_alert_incident", _spy)
    with caplog.at_level(logging.INFO):
        await main_mod.validate_calendar_oauth_production_on_startup()

    assert any(r.getMessage() == "calendar_oauth_production_ready" for r in caplog.records)
    assert alerts == []


@pytest.mark.asyncio
async def test_startup_non_production_does_not_alert(monkeypatch, caplog):
    monkeypatch.setattr(
        main_mod,
        "_calendar_oauth_production_status",
        lambda: (True, {"prod_ready": False, "reason": "non_production"}),
    )

    alerts: list[tuple] = []

    async def _spy(*, fingerprint, severity, summary):
        alerts.append((fingerprint, severity, summary))
        return (1, True, "sent")

    monkeypatch.setattr(main_mod, "_record_and_alert_incident", _spy)
    with caplog.at_level(logging.INFO):
        await main_mod.validate_calendar_oauth_production_on_startup()

    assert alerts == []
