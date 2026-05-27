"""Tests for the scheduler's thin httpx-based ``ApiClient`` wrapper."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

import services.scheduler.app.api_client as api_client_module
from services.scheduler.app.api_client import ApiClient


@pytest.fixture
def transport_factory(monkeypatch: pytest.MonkeyPatch):
    """Patch ``ApiClient``'s httpx.AsyncClient to use the supplied transport."""
    original = api_client_module.httpx.AsyncClient

    def make(handler):
        transport = httpx.MockTransport(handler)

        class _Patched(original):  # type: ignore[misc, valid-type]
            def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(
            api_client_module.httpx, "AsyncClient", _Patched
        )

    return make


def _api() -> ApiClient:
    return ApiClient(base_url="http://api:8000", service_token="tok")


@pytest.mark.asyncio
async def test_list_due_followups_sends_token_and_now(transport_factory) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        return httpx.Response(
            200, json={"rows": [{"id": 1, "chat_id": 42, "project_id": 1}]}
        )

    transport_factory(handler)
    now = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)
    rows = await _api().list_due_followups(now=now)
    assert rows == [{"id": 1, "chat_id": 42, "project_id": 1}]
    headers = captured["headers"]
    assert headers["authorization"] == "Bearer tok"  # type: ignore[index]
    url = httpx.URL(str(captured["url"]))
    assert url.params.get("now") == now.isoformat()


@pytest.mark.asyncio
async def test_skip_stale_posts_correct_path(transport_factory) -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, json={"ok": True})

    transport_factory(handler)
    await _api().skip_stale(99)
    assert captured["path"].endswith("/sales/followups/99/skip-stale")


@pytest.mark.asyncio
async def test_reschedule_posts_body(transport_factory) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(200, json={"ok": True})

    transport_factory(handler)
    when = datetime(2026, 5, 27, 10, 0, tzinfo=UTC)
    await _api().reschedule(77, new_fire_at=when)
    assert when.isoformat() in captured["body"]  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fire_returns_dict(transport_factory) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "sent": True})

    transport_factory(handler)
    result = await _api().fire(11)
    assert result == {"ok": True, "sent": True}


@pytest.mark.asyncio
async def test_fire_returns_empty_dict_when_non_object_payload(
    transport_factory,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    transport_factory(handler)
    result = await _api().fire(11)
    assert result == {}


@pytest.mark.asyncio
async def test_list_due_followups_filters_non_dict_rows(transport_factory) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rows": [{"id": 1}, "bad", 3]})

    transport_factory(handler)
    now = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)
    rows = await _api().list_due_followups(now=now)
    assert rows == [{"id": 1}]


@pytest.mark.asyncio
async def test_list_due_followups_handles_missing_rows_key(
    transport_factory,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    transport_factory(handler)
    now = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)
    rows = await _api().list_due_followups(now=now)
    assert rows == []
