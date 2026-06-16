from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from services.api.app.user_gateway_client import UserGatewayClient


def _mock_http(monkeypatch, *, response: Mock):
    http_client = AsyncMock()
    http_client.post = AsyncMock(return_value=response)
    cm = AsyncMock()
    cm.__aenter__.return_value = http_client
    cm.__aexit__.return_value = None
    monkeypatch.setattr(
        "services.api.app.user_gateway_client.httpx.AsyncClient",
        lambda timeout: cm,
    )
    return http_client


@pytest.mark.asyncio
async def test_api_user_gateway_client_send_message(monkeypatch):
    response = Mock()
    response.raise_for_status = Mock()
    response.json.return_value = {"delivered": True}
    http_post = _mock_http(monkeypatch, response=response)

    client = UserGatewayClient(base_url="http://user_gateway:8005/")
    result = await client.send_message(operator_id=3, chat_id=9001, text="hello")

    assert result == {"delivered": True}
    http_post.post.assert_awaited_once()
    call = http_post.post.await_args
    assert call.args[0] == "http://user_gateway:8005/messages/send"
    assert call.kwargs["json"] == {
        "operator_id": 3,
        "chat_id": 9001,
        "text": "hello",
    }


@pytest.mark.asyncio
async def test_api_user_gateway_client_raises_on_http_error(monkeypatch):
    request = httpx.Request("POST", "http://user_gateway:8005/messages/send")
    real_response = httpx.Response(503, request=request, json={"detail": "down"})
    response = Mock()
    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "down", request=request, response=real_response
    )
    _mock_http(monkeypatch, response=response)

    client = UserGatewayClient(base_url="http://user_gateway:8005")
    with pytest.raises(httpx.HTTPStatusError):
        await client.send_message(operator_id=1, chat_id=2, text="x")
