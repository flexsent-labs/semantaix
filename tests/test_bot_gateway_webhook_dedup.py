"""Story 12.31 — atomic webhook-entry idempotency on the Telegram ``update_id``.

Operator-command handlers (persona / ``/hitl_config`` / file library / the slow
NL-service LLM call) run in ``telegram_webhook`` BEFORE the customer-path
``persist_normalized_message`` dedup. A Telegram redelivery (it retries when the
webhook returns non-200 or exceeds its ~5s deadline) would therefore re-run a
slow operator command and double-act. ``WebhookUpdateClaimRepository`` is the
lock that closes that window: the first delivery to claim an ``update_id`` wins
(returns True); a redelivery loses (returns False) and is dropped at entry.

The store is deliberately *non-transcript* — it lives in its own
``webhook_update_claims`` table, never the ``messages``/``conversations``
transcript that feeds ``/knowledge/extract``.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from platform_common.settings import get_settings
from services.bot_gateway.app import main as bot_main
from services.bot_gateway.app.main import api_client, hitl_ticket_repository
from services.bot_gateway.app.main import app as bot_app
from services.bot_gateway.app.webhook_dedup import WebhookUpdateClaimRepository


def test_claim_first_wins_subsequent_lose(tmp_path):
    repo = WebhookUpdateClaimRepository(str(tmp_path / "dedup.sqlite3"))
    assert repo.claim(726742793) is True
    # Same update_id again → already claimed (a Telegram redelivery).
    assert repo.claim(726742793) is False
    assert repo.claim(726742793) is False
    # A different update_id is independent.
    assert repo.claim(726742794) is True


def test_claim_survives_reinit_same_db(tmp_path):
    # A claim persists across repository re-instantiation (named-volume /
    # restart parity): a redelivery after a bot_gateway restart still dedups.
    db = str(tmp_path / "dedup.sqlite3")
    assert WebhookUpdateClaimRepository(db).claim(5001) is True
    assert WebhookUpdateClaimRepository(db).claim(5001) is False


def test_claim_store_is_dedicated_and_non_transcript(tmp_path):
    """The claim store is dedicated and non-transcript.

    Leaking operator-command text into the ``messages``/``conversations``
    transcript that feeds ``/knowledge/extract`` is exactly the failure mode
    that ruled out the naive "persist first" fix. The dedup store is its own
    DB file holding only ``webhook_update_claims`` — no transcript tables.
    """
    db_path = tmp_path / "dedup.sqlite3"
    repo = WebhookUpdateClaimRepository(str(db_path))
    assert repo.claim(4242) is True

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        claimed = connection.execute(
            "SELECT COUNT(*) FROM webhook_update_claims"
        ).fetchone()[0]
    assert tables == {"webhook_update_claims"}
    assert "messages" not in tables
    assert "conversations" not in tables
    assert claimed == 1


@pytest.fixture
def _operator_env(tmp_path, monkeypatch):
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    monkeypatch.setenv("PERSISTENCE_DB_PATH", str(tmp_path / "persistence.sqlite3"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _operator_nl_payload(*, text: str, update_id: int) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": 1, "username": "operator"},
            "chat": {"id": 1, "type": "private"},
            "text": text,
        },
    }


def test_operator_nl_command_redelivery_acts_once(_operator_env, monkeypatch):
    """The headline scenario: a slow operator NL-service command that Telegram
    redelivers must run its handler — including the OpenRouter call and the
    service-add side effect — exactly ONCE. The redelivery is dropped at the
    webhook entry claim with ``reason=duplicate_update`` before any handler.
    """
    monkeypatch.setattr(
        api_client,
        "find_operator_by_username",
        AsyncMock(
            return_value={
                "username": "@operator",
                "chat_id": 1,
                "project_id": 9,
                "is_active": True,
            }
        ),
    )
    add_svc = AsyncMock(return_value={"id": 77})
    monkeypatch.setattr(api_client, "add_sales_service", add_svc)

    async def fake_send_dm(chat_id: int, text: str) -> None:
        pass

    monkeypatch.setattr(bot_main, "_send_dm", fake_send_dm)
    fake_complete = AsyncMock(
        return_value={"action": "add", "name": "тур", "description": None}
    )
    monkeypatch.setattr(
        bot_main.operator_service_nl_openrouter, "complete_json", fake_complete
    )

    client = TestClient(bot_app)
    payload = _operator_nl_payload(text="добавь услугу тур", update_id=8200)

    first = client.post("/telegram/webhook", json=payload)
    second = client.post("/telegram/webhook", json=payload)

    assert first.status_code == 200
    assert first.json().get("route") == "service_add"
    assert second.status_code == 200
    assert second.json()["status"] == "ignored"
    assert second.json()["reason"] == "duplicate_update"

    # The slow handler ran exactly once across both deliveries — no double-act.
    fake_complete.assert_awaited_once()
    add_svc.assert_awaited_once()
