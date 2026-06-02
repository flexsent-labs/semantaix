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
async def test_startup_logs_when_a_model_is_unavailable(monkeypatch, caplog):
    monkeypatch.setattr(main_mod.openrouter_client, "_is_configured", lambda: True)

    async def _missing(*, client, models):
        return ["dead/model"]

    monkeypatch.setattr(main_mod, "find_unavailable_models", _missing)
    with caplog.at_level(logging.ERROR):
        await main_mod.validate_llm_models_on_startup()
    assert any(
        r.getMessage() == "llm_models_unavailable_at_startup" for r in caplog.records
    )


@pytest.mark.asyncio
async def test_startup_logs_ok_when_all_present(monkeypatch, caplog):
    monkeypatch.setattr(main_mod.openrouter_client, "_is_configured", lambda: True)

    async def _none(*, client, models):
        return []

    monkeypatch.setattr(main_mod, "find_unavailable_models", _none)
    with caplog.at_level(logging.INFO):
        await main_mod.validate_llm_models_on_startup()
    assert any(r.getMessage() == "llm_models_validated" for r in caplog.records)


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
