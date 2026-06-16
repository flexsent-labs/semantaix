from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from services.user_gateway.app.message_router import MessageRouter


class _ApiStub:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def forward_inbound(self, **kwargs):
        self.calls.append(kwargs)
        return {"ok": True}


def _event(
    *,
    is_private: bool = True,
    sender_username: str | None = "customer",
    chat_id: int = 101,
    text: str = "hello",
):
    sender = SimpleNamespace(username=sender_username, id=555)
    message = SimpleNamespace(message=text, text=text, chat_id=chat_id, sender=sender)
    return SimpleNamespace(is_private=is_private, sender=sender, message=message)


@pytest.mark.asyncio
async def test_message_router_ignores_non_private_events() -> None:
    queue: asyncio.Queue[object] = asyncio.Queue(maxsize=2)
    api = _ApiStub()
    router = MessageRouter(
        api_client=api,
        queue=queue,
        operator_id=1,
        linked_username="operator",
    )
    await router.handle_new_message(_event(is_private=False))
    assert queue.qsize() == 0
    assert api.calls == []


@pytest.mark.asyncio
async def test_message_router_filters_linked_operator_username() -> None:
    queue: asyncio.Queue[object] = asyncio.Queue(maxsize=2)
    api = _ApiStub()
    router = MessageRouter(
        api_client=api,
        queue=queue,
        operator_id=2,
        linked_username="@operator",
    )
    await router.handle_new_message(_event(sender_username="operator"))
    assert queue.qsize() == 0


@pytest.mark.asyncio
async def test_message_router_forwards_private_message_from_customer() -> None:
    queue: asyncio.Queue[object] = asyncio.Queue(maxsize=2)
    api = _ApiStub()
    router = MessageRouter(
        api_client=api,
        queue=queue,
        operator_id=3,
        linked_username="@operator",
    )
    await router.handle_new_message(
        _event(sender_username="customer_1", chat_id=303, text="hi there")
    )
    task = asyncio.create_task(router.drain_queue())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(api.calls) == 1
    call = api.calls[0]
    assert call["chat_id"] == 303
    assert call["text"] == "hi there"
    assert call["delivery_channel"] == "operator_user"
    assert call["operator_id"] == 3


@pytest.mark.asyncio
async def test_message_router_queue_full(caplog) -> None:
    queue: asyncio.Queue[object] = asyncio.Queue(maxsize=1)
    queue.put_nowait(object())
    router = MessageRouter(
        api_client=AsyncMock(),
        queue=queue,
        operator_id=1,
        linked_username=None,
    )
    with caplog.at_level("WARNING", logger="services.user_gateway.app.message_router"):
        await router.handle_new_message(_event())
    assert "user_gateway_queue_full" in caplog.text


@pytest.mark.asyncio
async def test_message_router_forward_error(caplog) -> None:
    api = AsyncMock()
    api.forward_inbound.side_effect = httpx.HTTPError("fail")
    router = MessageRouter(
        api_client=api,
        queue=asyncio.Queue(maxsize=2),
        operator_id=1,
        linked_username=None,
    )
    with caplog.at_level("WARNING", logger="services.user_gateway.app.message_router"):
        await router._forward(_event().message)
    assert "user_gateway_forward_failed" in caplog.text
