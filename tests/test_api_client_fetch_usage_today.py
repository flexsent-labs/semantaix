"""Tests for ApiClient.fetch_usage_today (Story 14.08)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from services.bot_gateway.app.api_client import ApiClient

_BASE = "http://api:8000"
_TOKEN = "test-token"
_TODAY = "2026-06-16"
_PROJECT_ID = 7


def _client() -> ApiClient:
    return ApiClient(base_url=_BASE, timeout_seconds=5, internal_token=_TOKEN)


_SUMMARY_BODY = {
    "rows": [
        {
            "tracker_type": "llm",
            "model_name": "claude-haiku-4-5",
            "prompt_tokens_total": 1000,
            "completion_tokens_total": 500,
            "cost_usd_total": 0.05,
            "wasted_cost_usd": None,
            "call_count": 3,
        }
    ]
}
_WASTED_BODY = {"rows": [{"tracker_type": "llm", "wasted_cost_usd": 0.01}]}


def _mock_response(body: dict, status: int = 200) -> httpx.Response:
    resp = httpx.Response(
        status,
        content=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )
    resp.request = httpx.Request("GET", "http://api:8000/api/usage/summary")
    return resp


def _make_async_client_mock(responses: list[httpx.Response]):
    """Build a mock AsyncClient that returns responses in order."""
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=responses)
    return mock_client


@pytest.mark.asyncio
async def test_operator_scope_calls_only_summary():
    mock_client = _make_async_client_mock([_mock_response(_SUMMARY_BODY)])
    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await _client().fetch_usage_today(
            project_id=_PROJECT_ID,
            scope="operator",
            as_user="@op",
            today_utc=_TODAY,
            internal_token=_TOKEN,
        )
    assert result is not None
    assert result["summary_rows"] == _SUMMARY_BODY["rows"]
    assert result["wasted_rows"] is None
    # Only one GET call — no wasted endpoint
    assert mock_client.get.call_count == 1


@pytest.mark.asyncio
async def test_admin_scope_calls_summary_and_wasted():
    mock_client_summary = _make_async_client_mock([_mock_response(_SUMMARY_BODY)])
    mock_client_wasted = _make_async_client_mock([_mock_response(_WASTED_BODY)])
    call_count = 0
    clients = [mock_client_summary, mock_client_wasted]

    class _ClientFactory:
        def __new__(cls, **kwargs):
            nonlocal call_count
            c = clients[call_count]
            call_count += 1
            return c

    with patch("httpx.AsyncClient", _ClientFactory):
        result = await _client().fetch_usage_today(
            project_id=_PROJECT_ID,
            scope="admin",
            as_user="@admin",
            today_utc=_TODAY,
            internal_token=_TOKEN,
        )
    assert result is not None
    assert result["summary_rows"] == _SUMMARY_BODY["rows"]
    assert result["wasted_rows"] == _WASTED_BODY["rows"]


@pytest.mark.asyncio
async def test_correct_query_params_sent():
    captured_kwargs: dict = {}

    async def fake_get(url, **kwargs):
        captured_kwargs.update(kwargs)
        return _mock_response(_SUMMARY_BODY)

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = fake_get

    with patch("httpx.AsyncClient", return_value=mock_client):
        await _client().fetch_usage_today(
            project_id=_PROJECT_ID,
            scope="operator",
            as_user="@op-test",
            today_utc=_TODAY,
            internal_token=_TOKEN,
        )

    params = captured_kwargs["params"]
    assert params["project_id"] == _PROJECT_ID
    assert params["from_day_utc"] == _TODAY
    assert params["to_day_utc"] == _TODAY
    assert params["as_user"] == "@op-test"
    assert "Authorization" in captured_kwargs["headers"]
    assert _TOKEN in captured_kwargs["headers"]["Authorization"]


@pytest.mark.asyncio
async def test_request_error_returns_none():
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await _client().fetch_usage_today(
            project_id=_PROJECT_ID,
            scope="operator",
            as_user="@op",
            today_utc=_TODAY,
            internal_token=_TOKEN,
        )
    assert result is None


@pytest.mark.asyncio
async def test_503_returns_none():
    mock_client = _make_async_client_mock([_mock_response({}, status=503)])
    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await _client().fetch_usage_today(
            project_id=_PROJECT_ID,
            scope="operator",
            as_user="@op",
            today_utc=_TODAY,
            internal_token=_TOKEN,
        )
    assert result is None


@pytest.mark.asyncio
async def test_wasted_endpoint_receives_correct_params():
    captured_wasted_kwargs: dict = {}

    async def fake_get_wasted(url, **kwargs):
        if "wasted" in url:
            captured_wasted_kwargs.update(kwargs)
        return _mock_response(_WASTED_BODY)

    async def fake_get_summary(url, **kwargs):
        return _mock_response(_SUMMARY_BODY)

    call_count = 0

    class _Clients:
        def __new__(cls, **kwargs):
            nonlocal call_count
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            if call_count == 0:
                mock_client.get = fake_get_summary
            else:
                mock_client.get = fake_get_wasted
            call_count += 1
            return mock_client

    with patch("httpx.AsyncClient", _Clients):
        await _client().fetch_usage_today(
            project_id=_PROJECT_ID,
            scope="admin",
            as_user="@admin",
            today_utc=_TODAY,
            internal_token=_TOKEN,
        )

    params = captured_wasted_kwargs.get("params", {})
    assert params.get("from_day_utc") == _TODAY
    assert params.get("to_day_utc") == _TODAY
    assert params.get("project_id") == _PROJECT_ID


@pytest.mark.asyncio
async def test_non_gateway_error_propagates():
    """Non-502/503/504 HTTPStatusError (e.g. 401) is re-raised, not swallowed."""
    mock_client = _make_async_client_mock([_mock_response({}, status=401)])
    with patch("httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(httpx.HTTPStatusError):
            await _client().fetch_usage_today(
                project_id=_PROJECT_ID,
                scope="operator",
                as_user="@op",
                today_utc=_TODAY,
                internal_token=_TOKEN,
            )
