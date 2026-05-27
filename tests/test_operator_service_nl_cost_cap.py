"""Input-length cap before the LLM call.

The classifier runs on every authorized operator inbound that didn't match a
slash command. A long pasted message (e.g. an operator dumping a meeting
transcript into the bot DM) would otherwise be sent verbatim to OpenRouter
and cost the project an open-ended token bill. The cap is 500 chars, asserted
via the captured ``user`` kwarg on the LLM client.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from services.bot_gateway.app.operator_service_nl import classify_service_intent


class _FakeOpenRouter:
    def __init__(self) -> None:
        self.complete_json = AsyncMock(return_value={"action": None})


@pytest.mark.asyncio
async def test_short_input_passes_through_unchanged() -> None:
    fake = _FakeOpenRouter()
    text = "добавь услугу X"
    await classify_service_intent(text, openrouter=fake)
    assert fake.complete_json.await_args.kwargs["user"] == text


@pytest.mark.asyncio
async def test_exactly_500_chars_passes_through_unchanged() -> None:
    fake = _FakeOpenRouter()
    text = "x" * 500
    await classify_service_intent(text, openrouter=fake)
    assert fake.complete_json.await_args.kwargs["user"] == text


@pytest.mark.asyncio
async def test_input_longer_than_500_chars_is_truncated() -> None:
    fake = _FakeOpenRouter()
    text = "a" * 501
    await classify_service_intent(text, openrouter=fake)
    passed = fake.complete_json.await_args.kwargs["user"]
    assert len(passed) == 500
    assert passed == "a" * 500


@pytest.mark.asyncio
async def test_very_long_input_is_capped_at_500() -> None:
    fake = _FakeOpenRouter()
    text = "длинный текст " * 1000  # well past 500 chars
    await classify_service_intent(text, openrouter=fake)
    passed = fake.complete_json.await_args.kwargs["user"]
    assert len(passed) == 500
