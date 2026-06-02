"""Tests for the LLM model-availability guard (Story 12.41).

A retired OpenRouter slug 404s on /chat/completions and silently degrades the
bot (booking-dialog round-7). ``find_unavailable_models`` validates the
configured models against the live model list so a deprecation is caught loudly.
"""

from __future__ import annotations

import logging

import pytest

from services.api.app.llm_model_health import find_unavailable_models


class _FakeClient:
    def __init__(self, *, configured: bool = True, available=None, error=None):
        self._configured = configured
        self._available = set(available or set())
        self._error = error
        self.fetch_calls = 0

    def _is_configured(self) -> bool:
        return self._configured

    async def fetch_available_model_ids(self) -> set[str]:
        self.fetch_calls += 1
        if self._error is not None:
            raise self._error
        return set(self._available)


@pytest.mark.asyncio
async def test_all_models_present_returns_empty():
    client = _FakeClient(available={"openai/gpt-4o-mini", "google/gemini-2.5-flash-lite"})
    missing = await find_unavailable_models(
        client=client, models=["openai/gpt-4o-mini", "google/gemini-2.5-flash-lite"]
    )
    assert missing == []


@pytest.mark.asyncio
async def test_missing_model_detected_and_logged(caplog):
    client = _FakeClient(available={"openai/gpt-4o-mini"})
    with caplog.at_level(logging.ERROR):
        missing = await find_unavailable_models(
            client=client,
            models=["openai/gpt-4o-mini", "google/gemini-2.0-flash-lite-001"],
        )
    assert missing == ["google/gemini-2.0-flash-lite-001"]
    rec = next(r for r in caplog.records if r.getMessage() == "llm_model_unavailable")
    assert rec.model == "google/gemini-2.0-flash-lite-001"


@pytest.mark.asyncio
async def test_transport_error_returns_empty_and_warns(caplog):
    client = _FakeClient(error=RuntimeError("boom"))
    with caplog.at_level(logging.WARNING):
        missing = await find_unavailable_models(client=client, models=["a"])
    assert missing == []
    assert any(r.getMessage() == "llm_model_check_failed" for r in caplog.records)


@pytest.mark.asyncio
async def test_unconfigured_client_skips_network():
    client = _FakeClient(configured=False)
    missing = await find_unavailable_models(client=client, models=["a"])
    assert missing == []
    assert client.fetch_calls == 0  # never reaches the network in unit tests


@pytest.mark.asyncio
async def test_empty_models_returns_empty_without_network():
    client = _FakeClient(available={"a"})
    missing = await find_unavailable_models(client=client, models=[None, ""])
    assert missing == []
    assert client.fetch_calls == 0
