"""``ApiClient`` methods for the Story 12.02 sales-catalog endpoints.

Direct unit tests for the wire shape of ``add_sales_service`` /
``list_sales_services`` / ``delete_sales_service`` / ``get_sales_state``.
The dispatcher tests in
``tests/test_bot_gateway_sales_command_dispatch.py`` cover the high-level
behaviour; these tests pin the HTTP path + headers so a refactor doesn't
silently flip the request shape the api endpoints expect.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from services.bot_gateway.app.api_client import ApiClient, ApiError


def _http_mock(monkeypatch, *, response_json: dict, status_code: int = 200):
    response = Mock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = response_json
    if status_code >= 400:
        request = httpx.Request("POST", "http://api/x")
        response.raise_for_status = Mock(
            side_effect=httpx.HTTPStatusError(
                "boom",
                request=request,
                response=httpx.Response(status_code, request=request),
            )
        )
    else:
        response.raise_for_status = Mock()

    http_client = AsyncMock()
    http_client.post = AsyncMock(return_value=response)
    http_client.get = AsyncMock(return_value=response)
    http_client.delete = AsyncMock(return_value=response)

    cm = AsyncMock()
    cm.__aenter__.return_value = http_client
    cm.__aexit__.return_value = None
    monkeypatch.setattr(
        "services.bot_gateway.app.api_client.httpx.AsyncClient",
        lambda timeout: cm,
    )
    return http_client


@pytest.mark.asyncio
async def test_add_sales_service_posts_correct_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _http_mock(monkeypatch, response_json={"id": 12})
    client = ApiClient(base_url="http://api:8000")
    result = await client.add_sales_service(
        project_id=7,
        name="каньонинг",
        description_md="…",
        tags=["adventure"],
        internal_token="bot-tok",
    )
    assert result == {"id": 12}
    args = http.post.await_args
    assert args.args[0] == "http://api:8000/sales/services"
    assert args.kwargs["json"] == {
        "project_id": 7,
        "name": "каньонинг",
        "description_md": "…",
        "tags": ["adventure"],
    }
    assert args.kwargs["headers"] == {"Authorization": "Bearer bot-tok"}


@pytest.mark.asyncio
async def test_add_sales_service_raises_api_error_with_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = Mock(spec=httpx.Response)
    response.status_code = 409
    response.json.return_value = {"detail": "service_already_exists"}
    request = httpx.Request("POST", "http://api/x")
    response.raise_for_status = Mock(
        side_effect=httpx.HTTPStatusError(
            "boom",
            request=request,
            response=httpx.Response(
                409,
                request=request,
                content=b'{"detail": "service_already_exists"}',
            ),
        )
    )
    http_client = AsyncMock()
    http_client.post = AsyncMock(return_value=response)
    cm = AsyncMock()
    cm.__aenter__.return_value = http_client
    cm.__aexit__.return_value = None
    monkeypatch.setattr(
        "services.bot_gateway.app.api_client.httpx.AsyncClient",
        lambda timeout: cm,
    )
    client = ApiClient(base_url="http://api:8000")
    with pytest.raises(ApiError) as exc:
        await client.add_sales_service(
            project_id=7,
            name="x",
            description_md=None,
            tags=None,
            internal_token="t",
        )
    assert exc.value.detail == "service_already_exists"


@pytest.mark.asyncio
async def test_list_sales_services_uses_get_with_project_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _http_mock(monkeypatch, response_json={"services": []})
    client = ApiClient(base_url="http://api:8000")
    result = await client.list_sales_services(
        project_id=7, internal_token="bot-tok"
    )
    assert result == {"services": []}
    args = http.get.await_args
    assert args.args[0] == "http://api:8000/sales/services"
    assert args.kwargs["params"] == {"project_id": 7}
    assert args.kwargs["headers"] == {"Authorization": "Bearer bot-tok"}


@pytest.mark.asyncio
async def test_delete_sales_service_uses_delete_verb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _http_mock(monkeypatch, response_json={"ok": True})
    client = ApiClient(base_url="http://api:8000")
    result = await client.delete_sales_service(
        service_id=12, internal_token="bot-tok"
    )
    assert result == {"ok": True}
    args = http.delete.await_args
    assert args.args[0] == "http://api:8000/sales/services/12"
    assert args.kwargs["headers"] == {"Authorization": "Bearer bot-tok"}


@pytest.mark.asyncio
async def test_get_sales_state_no_chat_id_omits_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _http_mock(monkeypatch, response_json={"states": []})
    client = ApiClient(base_url="http://api:8000")
    result = await client.get_sales_state(
        project_id=7, chat_id=None, internal_token="bot-tok"
    )
    assert result == {"states": []}
    args = http.get.await_args
    assert args.args[0] == "http://api:8000/sales/state"
    assert args.kwargs["params"] == {"project_id": 7}


@pytest.mark.asyncio
async def test_get_sales_state_with_chat_id_includes_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _http_mock(monkeypatch, response_json={"states": []})
    client = ApiClient(base_url="http://api:8000")
    await client.get_sales_state(
        project_id=7, chat_id=12345, internal_token="bot-tok"
    )
    args = http.get.await_args
    assert args.kwargs["params"] == {"project_id": 7, "chat_id": 12345}
