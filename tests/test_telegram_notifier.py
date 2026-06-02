from unittest.mock import AsyncMock, Mock

import pytest

from services.api.app.telegram_notifier import TelegramIncidentNotifier


def test_llm_model_unavailable_is_a_critical_event():
    # Story 12.42 — a retired/unavailable model is a critical infra problem the
    # admin must be DM'd about (it silently breaks every persona turn).
    assert (
        TelegramIncidentNotifier.is_critical_event(
            fingerprint="llm_model_unavailable", severity="critical"
        )
        is True
    )


@pytest.mark.asyncio
async def test_notify_if_critical_skips_non_critical():
    notifier = TelegramIncidentNotifier(
        bot_token="token",
        alert_chat_id="-1001234",
        alert_username="@ajdevy",
    )
    sent, status = await notifier.notify_if_critical(
        incident_id=1,
        fingerprint="provider429_spike",
        severity="warning",
        summary="not critical",
        occurrence_count=1,
    )
    assert sent is False
    assert status == "not_critical"


@pytest.mark.asyncio
async def test_notify_if_critical_requires_chat_id():
    notifier = TelegramIncidentNotifier(
        bot_token="token",
        alert_chat_id=None,
        alert_username="@ajdevy",
    )
    sent, status = await notifier.notify_if_critical(
        incident_id=1,
        fingerprint="provider429_spike",
        severity="critical",
        summary="critical",
        occurrence_count=1,
    )
    assert sent is False
    assert status == "missing_alert_chat_id"


@pytest.mark.asyncio
async def test_notify_if_critical_sends_telegram_message(monkeypatch):
    response = Mock()
    response.raise_for_status = Mock()

    http_client = AsyncMock()
    http_client.post.return_value = response

    async_client_cm = AsyncMock()
    async_client_cm.__aenter__.return_value = http_client
    async_client_cm.__aexit__.return_value = None
    monkeypatch.setattr(
        "services.api.app.telegram_notifier.httpx.AsyncClient",
        lambda timeout: async_client_cm,
    )

    notifier = TelegramIncidentNotifier(
        bot_token="token",
        alert_chat_id="-1001234",
        alert_username="@ajdevy",
    )
    sent, status = await notifier.notify_if_critical(
        incident_id=2,
        fingerprint="provider429_spike",
        severity="critical",
        summary="Provider returned too many requests",
        occurrence_count=3,
    )

    assert sent is True
    assert status == "sent"
    post_args = http_client.post.call_args.args
    post_kwargs = http_client.post.call_args.kwargs
    assert post_args[0] == "https://api.telegram.org/bottoken/sendMessage"
    assert post_kwargs["json"]["chat_id"] == "-1001234"
