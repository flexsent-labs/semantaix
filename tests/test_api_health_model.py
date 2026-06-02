"""/health/model endpoint + startup model-validation guard (Story 12.41)."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

import services.api.app.main as main_mod
from services.api.app.main import app as api_app


def test_health_model_ok_when_all_models_present(monkeypatch):
    async def _none(*, client, models):
        return []

    monkeypatch.setattr(main_mod, "find_unavailable_models", _none)
    resp = TestClient(api_app).get("/health/model")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["unavailable"] == []
    assert main_mod.settings.openrouter_grounding_model in body["configured"]


def test_health_model_503_when_a_model_is_unavailable(monkeypatch):
    async def _missing(*, client, models):
        return ["google/gemini-2.0-flash-lite-001"]

    monkeypatch.setattr(main_mod, "find_unavailable_models", _missing)
    resp = TestClient(api_app).get("/health/model")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["unavailable"] == ["google/gemini-2.0-flash-lite-001"]


@pytest.mark.asyncio
async def test_startup_alerts_admin_when_model_unavailable(monkeypatch, caplog):
    # Story 12.42 — a dead model raises a critical incident, which DMs the admin
    # (via the existing incident → notify → debounce path).
    monkeypatch.setattr(main_mod.openrouter_client, "_is_configured", lambda: True)

    async def _missing(*, client, models):
        return ["dead/model"]

    monkeypatch.setattr(main_mod, "find_unavailable_models", _missing)

    alerts: list[tuple] = []

    async def _spy(*, fingerprint, severity, summary):
        alerts.append((fingerprint, severity, summary))
        return (1, True, "sent")

    monkeypatch.setattr(main_mod, "_record_and_alert_incident", _spy)
    with caplog.at_level(logging.ERROR):
        await main_mod.validate_llm_models_on_startup()

    assert any(
        r.getMessage() == "llm_models_unavailable_at_startup" for r in caplog.records
    )
    assert len(alerts) == 1
    fingerprint, severity, summary = alerts[0]
    assert fingerprint == "llm_model_unavailable"
    assert severity == "critical"
    assert "dead/model" in summary


@pytest.mark.asyncio
async def test_startup_does_not_alert_when_all_present(monkeypatch, caplog):
    monkeypatch.setattr(main_mod.openrouter_client, "_is_configured", lambda: True)

    async def _none(*, client, models):
        return []

    monkeypatch.setattr(main_mod, "find_unavailable_models", _none)

    alerts: list[tuple] = []

    async def _spy(*, fingerprint, severity, summary):
        alerts.append((fingerprint, severity, summary))
        return (1, False, "not_critical")

    monkeypatch.setattr(main_mod, "_record_and_alert_incident", _spy)
    with caplog.at_level(logging.INFO):
        await main_mod.validate_llm_models_on_startup()

    assert any(r.getMessage() == "llm_models_validated" for r in caplog.records)
    assert alerts == []  # no incident raised when everything is healthy


@pytest.mark.asyncio
async def test_startup_skips_network_when_unconfigured(monkeypatch):
    monkeypatch.setattr(main_mod.openrouter_client, "_is_configured", lambda: False)
    calls = {"n": 0}

    async def _spy(*, client, models):
        calls["n"] += 1
        return []

    monkeypatch.setattr(main_mod, "find_unavailable_models", _spy)
    await main_mod.validate_llm_models_on_startup()
    assert calls["n"] == 0
