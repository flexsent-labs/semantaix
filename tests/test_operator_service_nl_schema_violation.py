"""Schema-violation handling for the operator services NL classifier.

When the LLM returns non-JSON (or a JSON value that does not match our
schema), ``classify_service_intent`` MUST log a structured event and return
``None`` rather than propagate the exception. The dispatch hook in
bot_gateway/main.py is on the hot operator-message path; an unhandled
exception there would 5xx the Telegram webhook and trigger retries.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from services.api.app.openrouter_client import OpenRouterJsonSchemaViolation
from services.bot_gateway.app.operator_service_nl import classify_service_intent


class _FakeOpenRouter:
    def __init__(self, *, raise_exc: Exception | None = None, payload: object = None):
        if raise_exc is not None:
            self.complete_json = AsyncMock(side_effect=raise_exc)
        else:
            self.complete_json = AsyncMock(return_value=payload)


@pytest.mark.asyncio
async def test_non_json_response_returns_none_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    fake = _FakeOpenRouter(
        raise_exc=OpenRouterJsonSchemaViolation("non-JSON response: 'oops'")
    )
    intent = await classify_service_intent("добавь услугу X", openrouter=fake)
    assert intent is None
    events = [r for r in caplog.records if r.message == "operator_service_nl_schema_violation"]
    assert events, "expected operator_service_nl_schema_violation log"


@pytest.mark.asyncio
async def test_unknown_action_returns_none_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    fake = _FakeOpenRouter(payload={"action": "rename", "name": "X"})
    intent = await classify_service_intent("переименуй услугу", openrouter=fake)
    assert intent is None
    events = [r for r in caplog.records if r.message == "operator_service_nl_schema_violation"]
    assert events, "expected operator_service_nl_schema_violation log"


@pytest.mark.asyncio
async def test_missing_required_name_returns_none_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    fake = _FakeOpenRouter(payload={"action": "add", "name": None})
    intent = await classify_service_intent("добавь услугу", openrouter=fake)
    assert intent is None
    events = [r for r in caplog.records if r.message == "operator_service_nl_schema_violation"]
    assert events, "expected operator_service_nl_schema_violation log"


@pytest.mark.asyncio
async def test_schema_violation_does_not_propagate_exception() -> None:
    """The dispatch path is on the Telegram webhook hot path; raising would
    5xx the webhook and Telegram would retry → duplicate operator acks."""
    fake = _FakeOpenRouter(raise_exc=OpenRouterJsonSchemaViolation("bad"))
    # No try/except needed: the call must not raise.
    result = await classify_service_intent("текст", openrouter=fake)
    assert result is None


@pytest.mark.asyncio
async def test_runtime_error_from_missing_api_key_falls_through(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``OpenRouterClient`` raises ``RuntimeError`` when ``OPENROUTER_API_KEY``
    is unset. The webhook still needs to respond 200, so the classifier
    must swallow the error and let the rest of the inbound pipeline run."""
    caplog.set_level(logging.WARNING)
    fake = _FakeOpenRouter(
        raise_exc=RuntimeError("OPENROUTER_API_KEY is not configured")
    )
    result = await classify_service_intent("добавь услугу x", openrouter=fake)
    assert result is None
    events = [r for r in caplog.records if r.message == "operator_service_nl_llm_error"]
    assert events, "expected operator_service_nl_llm_error log"


@pytest.mark.asyncio
async def test_httpx_transport_error_falls_through() -> None:
    """OpenRouter network failure → classifier returns None instead of
    propagating an unhandled httpx exception into the webhook handler."""
    import httpx

    fake = _FakeOpenRouter(raise_exc=httpx.ConnectError("upstream down"))
    result = await classify_service_intent("добавь услугу x", openrouter=fake)
    assert result is None
