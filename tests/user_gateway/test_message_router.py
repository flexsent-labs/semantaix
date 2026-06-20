from __future__ import annotations

import asyncio
import uuid
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
    message_id: int | None = None,
):
    sender = SimpleNamespace(username=sender_username, id=555)
    message = SimpleNamespace(message=text, text=text, chat_id=chat_id, sender=sender)
    if message_id is not None:
        message.id = message_id
    return SimpleNamespace(is_private=is_private, sender=sender, message=message)


class _RateLimiter:
    def __init__(self, *, allowed: bool) -> None:
        self._allowed = allowed
        self.calls: list[dict[str, object]] = []

    def check_and_record(
        self, *, chat_id: int, now, max_messages: int, window_seconds: int
    ) -> bool:
        self.calls.append(
            {
                "chat_id": chat_id,
                "max_messages": max_messages,
                "window_seconds": window_seconds,
            }
        )
        return self._allowed


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
async def test_message_router_derives_deterministic_trace_id() -> None:
    # A1: re-delivered MTProto messages must reuse one trace_id so the api's
    # trace_id-keyed idempotency can deduplicate them.
    api = _ApiStub()
    router = MessageRouter(
        api_client=api,
        queue=asyncio.Queue(maxsize=2),
        operator_id=9,
        linked_username=None,
    )
    message = _event(chat_id=404, message_id=12345).message
    await router._forward(message)
    await router._forward(message)
    assert api.calls[0]["trace_id"] == "tg-user-9-404-12345"
    assert api.calls[1]["trace_id"] == "tg-user-9-404-12345"


@pytest.mark.asyncio
async def test_message_router_trace_id_falls_back_to_uuid() -> None:
    # No Telethon message id → random uuid (no idempotency, but never a crash).
    api = _ApiStub()
    router = MessageRouter(
        api_client=api,
        queue=asyncio.Queue(maxsize=2),
        operator_id=9,
        linked_username=None,
    )
    await router._forward(_event(chat_id=404).message)
    trace_id = api.calls[0]["trace_id"]
    assert not str(trace_id).startswith("tg-user-")
    uuid.UUID(str(trace_id))  # parses → valid uuid


@pytest.mark.asyncio
async def test_message_router_rate_limited_drops(caplog) -> None:
    # A2: over-budget customer messages are dropped before enqueue (no LLM hit).
    queue: asyncio.Queue[object] = asyncio.Queue(maxsize=2)
    api = _ApiStub()
    limiter = _RateLimiter(allowed=False)
    router = MessageRouter(
        api_client=api,
        queue=queue,
        operator_id=1,
        linked_username=None,
        rate_limiter=limiter,
        rate_limit_messages=3,
        rate_limit_window_seconds=60,
    )
    with caplog.at_level("WARNING", logger="services.user_gateway.app.message_router"):
        await router.handle_new_message(_event(chat_id=77))
    assert queue.qsize() == 0
    assert "user_gateway_rate_limited" in caplog.text
    assert limiter.calls[0] == {
        "chat_id": 555,
        "max_messages": 3,
        "window_seconds": 60,
    }


@pytest.mark.asyncio
async def test_message_router_rate_limited_allows_within_budget() -> None:
    queue: asyncio.Queue[object] = asyncio.Queue(maxsize=2)
    limiter = _RateLimiter(allowed=True)
    router = MessageRouter(
        api_client=_ApiStub(),
        queue=queue,
        operator_id=1,
        linked_username=None,
        rate_limiter=limiter,
    )
    await router.handle_new_message(_event(chat_id=88))
    assert queue.qsize() == 1


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
