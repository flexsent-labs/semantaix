from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from platform_common.settings import get_settings
from services.bot_gateway.app import main as bot_main
from services.bot_gateway.app.main import api_client, hitl_ticket_repository
from services.bot_gateway.app.main import app as bot_app


def _stub_operator_lookup(monkeypatch, *, record):
    async def fake_lookup(*, username: str):
        return record
    monkeypatch.setattr(api_client, "find_operator_by_username", fake_lookup)


@pytest.fixture(autouse=True)
def _isolate_hitl(tmp_path, monkeypatch):
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    persistence_path = tmp_path / "persistence.sqlite3"
    monkeypatch.setenv("PERSISTENCE_DB_PATH", str(persistence_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _customer_payload(text: str = "Когда придёт возврат?") -> dict:
    return {
        "update_id": 7001,
        "message": {
            "message_id": 1,
            "from": {"id": 9001, "username": "customer"},
            "chat": {"id": 9001, "type": "private"},
            "text": text,
        },
    }


def _operator_payload(
    *,
    text: str,
    reply_to_text: str | None,
    username: str = "operator",
) -> dict:
    msg: dict = {
        "message_id": 99,
        "from": {"id": 1, "username": username},
        "chat": {"id": 1, "type": "private"},
        "text": text,
    }
    if reply_to_text is not None:
        msg["reply_to_message"] = {"text": reply_to_text}
    return {"update_id": 7100, "message": msg}


def test_customer_message_is_forwarded_to_api_inbound(monkeypatch):
    forward = AsyncMock(return_value={"escalated": True})
    monkeypatch.setattr(api_client, "forward_inbound", forward)

    client = TestClient(bot_app)
    response = client.post("/telegram/webhook", json=_customer_payload())
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"

    # forward_inbound now runs as a BackgroundTask; TestClient executes
    # background tasks before returning, so the awaited count is still 1.
    forward.assert_awaited_once()
    kwargs = forward.await_args.kwargs
    assert kwargs["text"] == "Когда придёт возврат?"
    assert kwargs["chat_id"] == 9001
    assert kwargs["customer_username"] == "@customer"
    # trace_id is now deterministic, derived from update_id.
    assert kwargs["trace_id"] == "tg-update-7001"


def test_customer_forward_failure_does_not_leak_to_webhook_response(monkeypatch):
    """forward_inbound runs in BackgroundTasks; its failure must not change
    the synchronous webhook response, which has already been sent to
    Telegram. The handler logs the error instead. This is the bug-fix
    behaviour — the old shape returned ``forward: failed`` synchronously,
    which only worked because the forward was awaited inline."""
    monkeypatch.setattr(
        api_client, "forward_inbound", AsyncMock(side_effect=RuntimeError("boom"))
    )
    client = TestClient(bot_app)
    response = client.post("/telegram/webhook", json=_customer_payload())
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    # No "forward" marker — the failure is swallowed by _forward_inbound_safe.
    assert "forward" not in response.json()


def test_operator_reply_with_ticket_quote_routes_to_api(monkeypatch):
    _stub_operator_lookup(
        monkeypatch,
        record={"username": "@operator", "chat_id": 1, "project_id": 1, "is_active": True},
    )
    monkeypatch.setattr(api_client, "forward_inbound", AsyncMock(return_value={}))
    deliver = AsyncMock(return_value={"delivered": True, "resolved": True})
    monkeypatch.setattr(api_client, "deliver_operator_reply", deliver)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(
            text="В течение 5 рабочих дней.",
            reply_to_text="HITL ticket #42 | from @customer | Когда возврат?",
        ),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "operator_reply_delivered"
    assert body["ticket_id"] == "42"
    deliver.assert_awaited_once_with(
        ticket_id=42,
        operator_username="@operator",
        reply_text="В течение 5 рабочих дней.",
    )


def test_operator_reply_without_quote_uses_single_open_ticket(monkeypatch):
    _stub_operator_lookup(
        monkeypatch,
        record={"username": "@operator", "chat_id": 1, "project_id": 1, "is_active": True},
    )
    ticket = hitl_ticket_repository.create(
        conversation_ref="q", reason="awaiting_human_response", target_chat_id=9001
    )
    hitl_ticket_repository.assign(ticket_id=ticket.id, operator_username="@operator")
    deliver = AsyncMock(return_value={"delivered": True})
    monkeypatch.setattr(api_client, "deliver_operator_reply", deliver)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(text="ответ оператора", reply_to_text=None),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "operator_reply_delivered"
    deliver.assert_awaited_once_with(
        ticket_id=ticket.id,
        operator_username="@operator",
        reply_text="ответ оператора",
    )


def test_operator_reply_without_quote_and_multiple_tickets_requests_disambiguation(
    monkeypatch,
):
    """With 2+ assigned tickets and no quoted ticket reference, the gateway
    must DM the operator a disambiguation prompt. Silently dropping the
    reply (the historical behaviour) would hide the operator's response
    from the customer, which is the worst-case failure mode."""
    _stub_operator_lookup(
        monkeypatch,
        record={"username": "@operator", "chat_id": 1, "project_id": 1, "is_active": True},
    )
    for i in range(2):
        t = hitl_ticket_repository.create(
            conversation_ref=f"q{i}", reason="r", target_chat_id=1000 + i
        )
        hitl_ticket_repository.assign(ticket_id=t.id, operator_username="@operator")
    deliver = AsyncMock(return_value={"delivered": True})
    monkeypatch.setattr(api_client, "deliver_operator_reply", deliver)
    safe_send = AsyncMock()
    monkeypatch.setattr(bot_main, "_safe_send_text", safe_send)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(text="который из ответов?", reply_to_text=None),
    )
    body = response.json()
    assert body["status"] == "operator_reply_disambiguation_requested"
    assert body["open_ticket_count"] == "2"
    deliver.assert_not_awaited()
    safe_send.assert_awaited_once()
    dm_text = safe_send.await_args.kwargs["text"]
    assert "HITL ticket #" in dm_text
    assert "q0" in dm_text
    assert "q1" in dm_text


def test_operator_reply_without_quote_and_no_open_tickets_is_ignored(monkeypatch):
    """With zero assigned tickets there's nothing to disambiguate; the
    handler logs+ignores rather than DMing the operator about an empty
    list."""
    _stub_operator_lookup(
        monkeypatch,
        record={"username": "@operator", "chat_id": 1, "project_id": 1, "is_active": True},
    )
    deliver = AsyncMock(return_value={"delivered": True})
    monkeypatch.setattr(api_client, "deliver_operator_reply", deliver)
    safe_send = AsyncMock()
    monkeypatch.setattr(bot_main, "_safe_send_text", safe_send)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(text="ответ на что?", reply_to_text=None),
    )
    body = response.json()
    assert body["status"] == "ignored"
    assert body["reason"] == "operator_reply_unmatched"
    deliver.assert_not_awaited()
    safe_send.assert_not_awaited()


def test_operator_admin_command_takes_precedence_over_reply_branch(monkeypatch):
    # Even when the sender is the admin, a /hitl_config command must be
    # handled by the admin branch (not the operator reply branch).
    import services.bot_gateway.app.main as bot_main

    monkeypatch.setattr(
        bot_main.settings, "hitl_config_admin_username", "@ajdevy"
    )
    monkeypatch.setattr(api_client, "attach_operator", AsyncMock(return_value={}))
    deliver = AsyncMock(return_value={"delivered": True})
    monkeypatch.setattr(api_client, "deliver_operator_reply", deliver)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(
            text="/hitl_config @new_op 555",
            reply_to_text=None,
            username="ajdevy",
        ),
    )
    assert response.json()["status"] == "configured"
    deliver.assert_not_awaited()


def test_operator_reply_api_failure_is_reported(monkeypatch):
    _stub_operator_lookup(
        monkeypatch,
        record={"username": "@operator", "chat_id": 1, "project_id": 1, "is_active": True},
    )
    monkeypatch.setattr(
        api_client,
        "deliver_operator_reply",
        AsyncMock(side_effect=RuntimeError("api down")),
    )

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(
            text="answer",
            reply_to_text="HITL ticket #7 | from @c | q",
        ),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["reason"] == "operator_reply_delivery_failed"


def test_non_operator_message_is_not_routed_to_reply_branch(monkeypatch):
    _stub_operator_lookup(monkeypatch, record=None)
    deliver = AsyncMock(return_value={})
    forward = AsyncMock(return_value={})
    monkeypatch.setattr(api_client, "deliver_operator_reply", deliver)
    monkeypatch.setattr(api_client, "forward_inbound", forward)

    client = TestClient(bot_app)
    response = client.post("/telegram/webhook", json=_customer_payload())
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    deliver.assert_not_awaited()
    forward.assert_awaited_once()


def test_extract_ticket_id_handles_various_formats():
    assert bot_main._extract_ticket_id("HITL ticket #42 | x") == 42
    assert bot_main._extract_ticket_id("hitl ticket  #007") == 7
    assert bot_main._extract_ticket_id(None) is None
    assert bot_main._extract_ticket_id("no ticket here") is None


def test_telegram_retry_short_circuits_to_single_forward(monkeypatch):
    """The bug-fix regression: 3 webhook POSTs with the same update_id /
    message_id must produce exactly one forward to the api. Story 12.31 now
    claims the update_id at the webhook entry (before any handler), so the
    redeliveries short-circuit there with reason=duplicate_update; the
    UNIQUE(source_message_id) persist gate remains a second, finer layer."""
    forward = AsyncMock(return_value={"escalated": True})
    monkeypatch.setattr(api_client, "forward_inbound", forward)

    client = TestClient(bot_app)
    payload = _customer_payload()
    responses = [
        client.post("/telegram/webhook", json=payload).json() for _ in range(3)
    ]

    forward.assert_awaited_once()
    assert responses[0]["status"] == "accepted"
    assert responses[1]["status"] == "ignored"
    assert responses[1]["reason"] == "duplicate_update"
    assert responses[2]["status"] == "ignored"
    assert responses[2]["reason"] == "duplicate_update"
    # Same trace_id across all three so the api side would also dedup if
    # the bot_gateway short-circuit were ever bypassed.
    assert (
        responses[0]["trace_id"]
        == responses[1]["trace_id"]
        == responses[2]["trace_id"]
        == "tg-update-7001"
    )


def test_telegram_webhook_top_level_exception_returns_200_not_500(monkeypatch):
    """If a handler raises an unexpected exception, the webhook must still
    return 200 so Telegram does not retry. A 500 here was the upstream
    trigger that caused the triple-ack incident: pipeline timeouts +
    Telegram retries amplified one slow forward into three acks."""

    async def boom(*args, **kwargs):
        raise RuntimeError("unhandled bug deep in handler")

    monkeypatch.setattr(bot_main, "_handle_kb_command", boom)

    client = TestClient(bot_app)
    response = client.post("/telegram/webhook", json=_customer_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["handler"] == "failed"


def test_format_disambiguation_dm_truncates_long_snippets():
    """Operator-facing prompt clips conversation_ref to ~60 chars so a
    customer's long original question doesn't push the prompt past
    Telegram's message width / readability."""
    from dataclasses import dataclass

    @dataclass
    class FakeTicket:
        id: int
        conversation_ref: str

    long_ref = "когда я смогу снять багги " * 5  # 130 chars
    tickets = [
        FakeTicket(id=42, conversation_ref=long_ref),
        FakeTicket(id=43, conversation_ref="short"),
    ]
    rendered = bot_main._format_disambiguation_dm(tickets)
    assert "HITL ticket #42" in rendered
    assert "HITL ticket #43" in rendered
    assert "…" in rendered  # long_ref was truncated
    # The full long ref must not appear verbatim — only the truncated form.
    assert long_ref not in rendered


def test_derive_trace_id_prefers_header_then_update_id_then_uuid():
    """trace_id derivation contract: deterministic from update_id when
    Telegram retries (so api-side idempotency on /conversations/inbound
    can short-circuit), but yields to an explicit X-Trace-Id header for
    operator-side scripts."""
    assert bot_main._derive_trace_id(header_trace="abc", update_id=7) == "abc"
    assert bot_main._derive_trace_id(header_trace=None, update_id=42) == "tg-update-42"
    assert bot_main._derive_trace_id(header_trace="", update_id=42) == "tg-update-42"
    # Missing update_id falls back to a generated uuid (still non-empty).
    fallback = bot_main._derive_trace_id(header_trace=None, update_id=None)
    assert isinstance(fallback, str) and len(fallback) >= 8
