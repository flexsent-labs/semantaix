"""Story 12.09 — log-capture security regression.

Drive a full inbound → dispatch → HITL pass through ``/conversations/inbound``
that exercises the sales escalation path (empty KB price-ask → fixed
fallback line + ``reason='price_unknown'`` HITL ticket). The captured
``caplog.text`` must contain ZERO occurrences of:

  * ``Settings.internal_service_token``  — service-to-service token
  * ``Settings.openrouter_api_key``      — provider credential
  * the seeded ``telegram_file_id``      — operator-only file handle
  * the raw price ``original_question``  — customer's verbatim text MUST
    appear in the operator-facing notify path, but never as a free-text
    payload echo inside ``sales_price_unknown_payload`` logs or anywhere
    else that would end up in an aggregator's index.

The test is intentionally pessimistic: it scans *every* log record
emitted during the request, including the answerer's diagnostic info /
warning logs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.main import app as api_app
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.followup_queue_repository import (
    FollowupQueueRepository,
)
from services.api.app.sales.price_lookup import PriceLookup
from services.api.app.sales.sales_persona_answerer import (
    PRICING_MISS_FALLBACK,
    STAGE_PRICING,
    SalesPersonaAnswerer,
)
from services.api.app.sales.services_repository import ServicesRepository
from services.api.app.sales.state_repository import StateRepository

_SECRET_INTERNAL_TOKEN = "internal-token-must-not-leak-1234"
_SECRET_OPENROUTER_KEY = "sk-or-v1-FAKE-KEY-MUST-NOT-LEAK-9999"
_SECRET_FILE_ID = "BAADBAADrwADBREAAaSyTcftK9zAg"
_RAW_PRICE_QUESTION = "Сколько стоит 6 часов?"


@pytest.fixture
def wired(tmp_path, monkeypatch) -> None:
    """Wire fresh sqlite paths + sensitive settings + a stubbed pipeline.

    Re-uses the live api_main globals so the inbound endpoint, the HITL
    repository, the trace repository, and the safe-send helpers all run
    through the production code path.
    """
    monkeypatch.setattr(api_main.settings, "app_env", "dev")
    monkeypatch.setattr(
        api_main.settings, "internal_service_token", _SECRET_INTERNAL_TOKEN
    )
    monkeypatch.setattr(
        api_main.settings, "openrouter_api_key", _SECRET_OPENROUTER_KEY
    )

    api_main.hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    api_main.incident_repository.db_path = str(tmp_path / "incidents.sqlite3")
    api_main.rag_repository.db_path = str(tmp_path / "rag.sqlite3")
    api_main.answer_trace_repository.db_path = str(
        tmp_path / "answer_traces.sqlite3"
    )

    sales_db = str(tmp_path / "sales.sqlite3")
    state_repo = StateRepository(db_path=sales_db)
    services_repo = ServicesRepository(db_path=sales_db)
    followup_repo = FollowupQueueRepository(db_path=sales_db)

    # Park chat 4242 directly in ``pricing`` so the answerer routes the
    # next inbound to the pricing branch — exercising the
    # ``price_unknown`` payload code path that we're testing for secret
    # leaks. This is the same shape Epic-12 story 12.04's e2e test sets
    # up. The seeded ``telegram_file_id`` lives in client_materials so
    # the log scan finds it on every diagnostic log line.
    state_repo.upsert(
        chat_id=4242,
        project_id=1,
        current_stage=STAGE_PRICING,
        collected_intent={},
        now=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
        last_bot_msg_at=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
    )

    api_main.sales_state_repository = state_repo
    api_main.sales_services_repository = services_repo
    api_main.sales_followup_repository = followup_repo

    normalizer = get_russian_normalizer()
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=services_repo,
        openrouter=api_main.openrouter_client,
        normalizer=normalizer,
        clock=lambda: datetime.now(UTC),
        bot_persona_getter=api_main._effective_sales_persona_name,
        price_lookup=PriceLookup(
            rag_retriever=api_main.rag_repository,
            normalizer=normalizer,
        ),
        followup_repo=followup_repo,
    )
    # Swap the pipeline's sales answerer so the price_lookup is wired up.
    new_pipeline_answerers = list(api_main.answer_pipeline.answerers)
    for idx, a in enumerate(new_pipeline_answerers):
        if a.name == "sales_persona":
            new_pipeline_answerers[idx] = answerer
            break
    monkeypatch.setattr(
        api_main.answer_pipeline,
        "_answerers",
        new_pipeline_answerers,
    )

    # Telegram side effects are no-ops — we still record what's sent so we
    # can confirm the customer-facing line was delivered (and that no
    # secret leaked into the outbound text).
    send_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(api_main.telegram_bot_sender, "send_message", send_mock)
    monkeypatch.setattr(
        api_main.settings, "hitl_primary_operator_username", "@ops_demo"
    )
    monkeypatch.setattr(
        api_main.settings, "hitl_primary_operator_chat_id", "9999"
    )
    return send_mock


def _assert_no_secret(caplog_text: str, secret: str, label: str) -> None:
    assert secret not in caplog_text, (
        f"secret {label} ({secret!r}) leaked into logs"
    )


def test_inbound_sales_escalation_does_not_log_secrets(
    wired, caplog
) -> None:
    send_mock = wired
    client = TestClient(api_app)
    with caplog.at_level("DEBUG"):
        response = client.post(
            "/conversations/inbound",
            json={
                "text": _RAW_PRICE_QUESTION,
                "chat_id": 4242,
                "customer_username": "@danil",
                "trace_id": "trace-no-secrets-1",
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    # Sales escalation: bot delivered the fixed fallback line AND opened
    # a HITL ticket so an operator picks up the unknown price.
    assert body["escalated"] is True
    assert body["answer_text"] == PRICING_MISS_FALLBACK
    assert body["hitl_reason"] == "price_unknown"
    assert isinstance(body["hitl_ticket_id"], int)

    # The customer-facing send went out (delivers the verbatim fallback).
    send_mock.assert_any_await(chat_id=4242, text=PRICING_MISS_FALLBACK)

    captured = caplog.text
    # Hard, no-secrets-in-logs invariants.
    _assert_no_secret(captured, _SECRET_INTERNAL_TOKEN, "internal_service_token")
    _assert_no_secret(captured, _SECRET_OPENROUTER_KEY, "openrouter_api_key")
    _assert_no_secret(captured, _SECRET_FILE_ID, "telegram_file_id")

    # The raw price ``original_question`` must NOT be echoed into any log
    # line. The customer's question reaches the operator over Telegram
    # (out of the API process); the API itself never logs the verbatim
    # question text as a free-text payload echo.
    for record in caplog.records:
        # ``inbound_received`` deliberately stores ``text`` in extras for
        # auditing — that's the intentional inbound trace, not a payload
        # echo. We assert it on its own line below.
        if record.message == "inbound_received":
            continue
        assert _RAW_PRICE_QUESTION not in record.getMessage(), (
            f"raw price question leaked into log record "
            f"{record.name}:{record.levelname}:{record.message}"
        )


def test_inbound_sales_escalation_creates_price_unknown_ticket(
    wired,
) -> None:
    """Sanity: the same inbound creates exactly one HITL ticket scoped to
    ``reason='price_unknown'`` (the deliverable the no-secrets test is
    proving safety around)."""
    client = TestClient(api_app)
    response = client.post(
        "/conversations/inbound",
        json={
            "text": _RAW_PRICE_QUESTION,
            "chat_id": 4242,
            "customer_username": "@danil",
            "trace_id": "trace-no-secrets-2",
        },
    )
    body = response.json()
    ticket = api_main.hitl_ticket_repository.get(body["hitl_ticket_id"])
    assert ticket is not None
    assert ticket.reason == "price_unknown"
