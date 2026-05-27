"""Classifier matrix tests for the operator services NL handler (Story 12.02b).

The classifier is a thin wrapper around ``OpenRouterClient.complete_json``: we
feed it a fake LLM that returns canned JSON shapes for each phrase the prompt
is documented to cover, and assert the returned ``ServiceIntent`` matches.

Goals:
- Lock down the public dataclass shape (action / name / description fields).
- Cover the four action types (add, remove, list, describe) plus the explicit
  no-classify ``action: null`` case.
- Cover the with-description and without-description variants of ``add``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from services.bot_gateway.app.operator_service_nl import (
    ServiceIntent,
    classify_service_intent,
)


class FakeOpenRouter:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.complete_json = AsyncMock(return_value=payload)


@pytest.mark.asyncio
async def test_classifier_returns_add_without_description() -> None:
    fake = FakeOpenRouter({"action": "add", "name": "Медовеевка Лайт", "description": None})
    intent = await classify_service_intent(
        "добавь услугу Медовеевка Лайт", openrouter=fake
    )
    assert intent == ServiceIntent(action="add", name="Медовеевка Лайт", description=None)


@pytest.mark.asyncio
async def test_classifier_returns_add_with_description() -> None:
    fake = FakeOpenRouter(
        {"action": "add", "name": "каньонинг", "description": "спуск по верёвке"}
    )
    intent = await classify_service_intent(
        "добавь услугу каньонинг — спуск по верёвке", openrouter=fake
    )
    assert intent == ServiceIntent(
        action="add", name="каньонинг", description="спуск по верёвке"
    )


@pytest.mark.asyncio
async def test_classifier_returns_remove() -> None:
    fake = FakeOpenRouter({"action": "remove", "name": "каньонинг", "description": None})
    intent = await classify_service_intent("удали услугу каньонинг", openrouter=fake)
    assert intent == ServiceIntent(action="remove", name="каньонинг", description=None)


@pytest.mark.asyncio
async def test_classifier_returns_list_with_no_name() -> None:
    fake = FakeOpenRouter({"action": "list", "name": None, "description": None})
    intent = await classify_service_intent("какие у нас услуги?", openrouter=fake)
    assert intent == ServiceIntent(action="list", name=None, description=None)


@pytest.mark.asyncio
async def test_classifier_returns_list_for_list_phrasing() -> None:
    fake = FakeOpenRouter({"action": "list"})
    intent = await classify_service_intent("список услуг", openrouter=fake)
    assert intent is not None
    assert intent.action == "list"
    assert intent.name is None
    assert intent.description is None


@pytest.mark.asyncio
async def test_classifier_returns_describe() -> None:
    fake = FakeOpenRouter(
        {"action": "describe", "name": "каньонинг", "description": "спуск по верёвке"}
    )
    intent = await classify_service_intent(
        "опиши каньонинг как спуск по верёвке", openrouter=fake
    )
    assert intent == ServiceIntent(
        action="describe", name="каньонинг", description="спуск по верёвке"
    )


@pytest.mark.asyncio
async def test_classifier_returns_none_when_action_is_null() -> None:
    fake = FakeOpenRouter({"action": None})
    intent = await classify_service_intent("послушай", openrouter=fake)
    assert intent is None


@pytest.mark.asyncio
async def test_classifier_returns_none_when_action_missing() -> None:
    fake = FakeOpenRouter({"name": "x"})
    intent = await classify_service_intent("чтото неопределённое", openrouter=fake)
    assert intent is None


@pytest.mark.asyncio
async def test_classifier_returns_none_for_unknown_action() -> None:
    fake = FakeOpenRouter({"action": "rename", "name": "x"})
    intent = await classify_service_intent("переименуй услугу", openrouter=fake)
    assert intent is None


@pytest.mark.asyncio
async def test_classifier_returns_none_when_add_has_no_name() -> None:
    fake = FakeOpenRouter({"action": "add", "name": None})
    intent = await classify_service_intent("добавь услугу", openrouter=fake)
    assert intent is None


@pytest.mark.asyncio
async def test_classifier_returns_none_when_remove_has_no_name() -> None:
    fake = FakeOpenRouter({"action": "remove", "name": None})
    intent = await classify_service_intent("удали услугу", openrouter=fake)
    assert intent is None


@pytest.mark.asyncio
async def test_classifier_returns_none_when_describe_has_no_name() -> None:
    fake = FakeOpenRouter({"action": "describe", "name": None, "description": "x"})
    intent = await classify_service_intent("опиши как X", openrouter=fake)
    assert intent is None


@pytest.mark.asyncio
async def test_classifier_returns_none_when_name_wrong_type() -> None:
    fake = FakeOpenRouter({"action": "add", "name": 123})
    intent = await classify_service_intent("добавь услугу 123", openrouter=fake)
    assert intent is None


@pytest.mark.asyncio
async def test_classifier_returns_none_when_description_wrong_type() -> None:
    fake = FakeOpenRouter({"action": "add", "name": "x", "description": ["a"]})
    intent = await classify_service_intent("добавь услугу x", openrouter=fake)
    assert intent is None


@pytest.mark.asyncio
async def test_classifier_passes_system_prompt_and_user_text() -> None:
    fake = FakeOpenRouter({"action": "list"})
    await classify_service_intent("список услуг", openrouter=fake)
    kwargs = fake.complete_json.await_args.kwargs
    assert "system" in kwargs
    assert isinstance(kwargs["system"], str)
    assert len(kwargs["system"]) > 0
    assert kwargs["user"] == "список услуг"
