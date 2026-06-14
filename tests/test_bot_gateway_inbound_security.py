"""Story 12.103 — inbound message length cap and per-user rate limiting.

Covers:
- Customer message exceeding inbound_max_message_chars is truncated before forward.
- Customer message exactly at the limit passes as-is.
- Allowed customer message is forwarded normally.
- Rate-limited customer message is NOT forwarded.
- Rate-limited customer message gets a polite reply.
- Operator messages are NOT subject to rate limiting.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from platform_common.settings import get_settings
from services.bot_gateway.app import main as bot_main
from services.bot_gateway.app.main import (
    _apply_inbound_length_cap,
    api_client,
    hitl_ticket_repository,
    settings,
    telegram_bot_sender,
)
from services.bot_gateway.app.main import (
    app as bot_app,
)
from services.bot_gateway.app.rate_limit_repository import InboundRateLimitRepository

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Redirect DB singletons to tmp files and clear settings cache.

    webhook_update_claim_repository and rate_limit_repo are handled by the
    global conftest._isolate_webhook_update_claims fixture; this fixture covers
    the remaining singletons specific to bot_gateway integration tests."""
    orig_hitl_db = hitl_ticket_repository.db_path
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    monkeypatch.setenv("PERSISTENCE_DB_PATH", str(tmp_path / "persistence.sqlite3"))
    monkeypatch.setenv("WEBHOOK_DEDUP_DB_PATH", str(tmp_path / "dedup.sqlite3"))
    get_settings.cache_clear()
    yield
    hitl_ticket_repository.db_path = orig_hitl_db
    get_settings.cache_clear()


def _customer_payload(*, text: str, update_id: int = 1001, chat_id: int = 55000) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 9999999999,
            "from": {"id": chat_id, "username": "customer"},
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
        },
    }


def _operator_payload(*, text: str, update_id: int = 2001, chat_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 9999999999,
            "from": {"id": chat_id, "username": "operator"},
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
        },
    }


def _stub_forward(monkeypatch) -> list[str]:
    """Capture text values forwarded to api_client.forward_inbound."""
    forwarded: list[str] = []

    async def _fake_forward(*, text, chat_id, customer_username, trace_id, timeout_seconds):
        forwarded.append(text)

    monkeypatch.setattr(api_client, "forward_inbound", _fake_forward)
    return forwarded


def _stub_sender(monkeypatch) -> list[str]:
    """Capture text values sent back via telegram_bot_sender."""
    sent: list[str] = []

    async def _fake_send(*, chat_id: int, text: str) -> int:
        sent.append(text)
        return 1

    monkeypatch.setattr(telegram_bot_sender, "send_message", _fake_send)
    return sent


# ---------------------------------------------------------------------------
# Unit tests for _apply_inbound_length_cap (synchronous helper — no TestClient)
# ---------------------------------------------------------------------------


def test_apply_inbound_length_cap_truncates():
    result = _apply_inbound_length_cap("A" * 50, 20, chat_id=1, trace_id="t1")
    assert result == "A" * 20


def test_apply_inbound_length_cap_at_limit_passes_through():
    result = _apply_inbound_length_cap("B" * 20, 20, chat_id=1, trace_id="t2")
    assert result == "B" * 20


def test_apply_inbound_length_cap_under_limit_passes_through():
    result = _apply_inbound_length_cap("C" * 5, 20, chat_id=1, trace_id="t3")
    assert result == "C" * 5


# ---------------------------------------------------------------------------
# Integration tests via TestClient
# ---------------------------------------------------------------------------


def test_long_message_truncated_before_forward(monkeypatch):
    """A message over inbound_max_message_chars is truncated to that limit."""
    monkeypatch.setattr(settings, "inbound_max_message_chars", 20)
    forwarded = _stub_forward(monkeypatch)
    _stub_sender(monkeypatch)

    client = TestClient(bot_app)
    long_text = "A" * 50
    client.post("/telegram/webhook", json=_customer_payload(text=long_text))

    assert len(forwarded) == 1
    assert forwarded[0] == "A" * 20


def test_message_at_limit_forwarded_as_is(monkeypatch):
    """A message exactly at the char limit is forwarded without truncation."""
    monkeypatch.setattr(settings, "inbound_max_message_chars", 20)
    forwarded = _stub_forward(monkeypatch)
    _stub_sender(monkeypatch)

    client = TestClient(bot_app)
    exact_text = "B" * 20
    client.post("/telegram/webhook", json=_customer_payload(text=exact_text, update_id=1002))

    assert len(forwarded) == 1
    assert forwarded[0] == exact_text


def test_allowed_customer_message_forwarded_normally(monkeypatch):
    """Within the rate limit, messages are forwarded normally."""
    monkeypatch.setattr(settings, "inbound_rate_limit_messages", 5)
    monkeypatch.setattr(settings, "inbound_rate_limit_window_seconds", 300)
    forwarded = _stub_forward(monkeypatch)
    _stub_sender(monkeypatch)

    client = TestClient(bot_app)
    resp = client.post(
        "/telegram/webhook",
        json=_customer_payload(text="Привет!", update_id=3001),
    )

    assert resp.status_code == 200
    assert len(forwarded) == 1


def test_rate_limited_message_not_forwarded(monkeypatch):
    """After exceeding the limit, messages are dropped and NOT forwarded."""
    monkeypatch.setattr(settings, "inbound_rate_limit_messages", 2)
    monkeypatch.setattr(settings, "inbound_rate_limit_window_seconds", 300)
    forwarded = _stub_forward(monkeypatch)
    _stub_sender(monkeypatch)

    client = TestClient(bot_app)
    # First 2 allowed
    client.post("/telegram/webhook", json=_customer_payload(text="msg 1", update_id=4001))
    client.post("/telegram/webhook", json=_customer_payload(text="msg 2", update_id=4002))
    # 3rd should be rate-limited
    resp = client.post(
        "/telegram/webhook",
        json=_customer_payload(text="msg 3", update_id=4003),
    )

    assert resp.status_code == 200
    # Only 2 forwards happened
    assert len(forwarded) == 2


def test_rate_limited_message_sends_reply(monkeypatch):
    """A rate-limited customer receives a polite reply."""
    monkeypatch.setattr(settings, "inbound_rate_limit_messages", 1)
    monkeypatch.setattr(settings, "inbound_rate_limit_window_seconds", 300)
    _stub_forward(monkeypatch)
    sent = _stub_sender(monkeypatch)

    client = TestClient(bot_app)
    client.post("/telegram/webhook", json=_customer_payload(text="first", update_id=5001))
    client.post("/telegram/webhook", json=_customer_payload(text="second", update_id=5002))

    # The 2nd message triggers a polite reply
    assert any(bot_main._RATE_LIMIT_REPLY in s for s in sent)


def test_operator_message_bypasses_rate_limit(monkeypatch):
    """Operator messages are not subject to rate limiting."""
    monkeypatch.setattr(settings, "inbound_rate_limit_messages", 0)
    monkeypatch.setattr(settings, "inbound_rate_limit_window_seconds", 300)

    # Register @operator so resolve_operator_for_sender identifies this sender.
    monkeypatch.setattr(
        api_client,
        "find_operator_by_username",
        AsyncMock(return_value={"username": "@operator", "chat_id": 1, "project_id": 1,
                               "is_active": True}),
    )

    # Operator reply needs an open ticket — stub the fallback lookup
    monkeypatch.setattr(
        bot_main,
        "_fallback_open_ticket_for_operator",
        lambda operator_username: None,
    )

    # We only want to confirm the operator path is taken, not the customer rate-limit path.
    # The operator route returns early without hitting the rate-limit repo at all.

    check_calls: list[int] = []
    original_check = InboundRateLimitRepository.check_and_record

    def _recording_check(self, *, chat_id, now, max_messages, window_seconds):
        check_calls.append(chat_id)
        return original_check(
            self, chat_id=chat_id, now=now, max_messages=max_messages, window_seconds=window_seconds
        )

    monkeypatch.setattr(InboundRateLimitRepository, "check_and_record", _recording_check)

    client = TestClient(bot_app)
    client.post(
        "/telegram/webhook",
        json=_operator_payload(text="/service_list", update_id=6001),
    )

    # Rate limit was NOT consulted for the operator message
    assert check_calls == []
