from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from services.bot_gateway.app.user_gateway_client import UserGatewayClient, UserGatewayError


def _mock_http(monkeypatch, *, method: str, response: Mock):
    http_client = AsyncMock()
    setattr(http_client, method, AsyncMock(return_value=response))
    cm = AsyncMock()
    cm.__aenter__.return_value = http_client
    cm.__aexit__.return_value = None
    monkeypatch.setattr(
        "services.bot_gateway.app.user_gateway_client.httpx.AsyncClient",
        lambda timeout: cm,
    )
    return http_client


def _response(status_code: int, body: dict):
    response = Mock()
    response.status_code = status_code
    response.json.return_value = body
    if status_code >= 400:
        request = httpx.Request("POST", "http://ug/x")
        real_response = httpx.Response(status_code, request=request, json=body)
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "boom", request=request, response=real_response
        )
    else:
        response.raise_for_status = Mock()
    return response


@pytest.mark.asyncio
async def test_user_gateway_client_happy_paths(monkeypatch):
    response = _response(200, {"status": "ok"})
    http_post = _mock_http(monkeypatch, method="post", response=response)
    client = UserGatewayClient(base_url="http://ug", internal_token="tok")
    assert await client.qr_start(operator_id=1) == {"status": "ok"}
    assert http_post.post.await_args.kwargs["headers"] == {"Authorization": "Bearer tok"}

    response_get = _response(200, {"phase": "idle"})
    http_get = _mock_http(monkeypatch, method="get", response=response_get)
    assert await client.status(operator_id=1) == {"phase": "idle"}
    assert http_get.get.await_args.kwargs["params"] == {"operator_id": 1}


@pytest.mark.asyncio
async def test_user_gateway_client_raises_structured_error(monkeypatch):
    response = _response(404, {"detail": "operator_not_connected"})
    _mock_http(monkeypatch, method="post", response=response)
    client = UserGatewayClient(base_url="http://ug", internal_token="tok")
    with pytest.raises(UserGatewayError) as info:
        await client.send_message(operator_id=1, chat_id=2, text="hi")
    assert info.value.detail == "operator_not_connected"
