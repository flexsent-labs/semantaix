"""Story 12.09 — sales-escalation coalesce branch coverage.

Covers ``_dispatch_sales_escalation`` in ``services.api.app.main`` for the
case where a second sales-escalation inbound arrives on a chat that
already has an open HITL ticket. The dispatcher must:

  * NOT create a new HITL ticket.
  * Re-notify the assigned operator with a ``[follow-up]`` prefix so the
    rapid customer drift is delivered onto the same conversation.
  * Persist an ``answer_trace`` whose ``limitations`` list contains
    ``"coalesced_sales_followup"``.
  * Return ``coalesced=True`` in the response body.

This is the regression net for the coalesce-on-active-ticket invariant —
it defends against accidental re-introduction of N parallel HITL tickets
per customer when the bot escalates more than once in quick succession.
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

_CHAT_ID = 5151
_PROJECT_ID = 1
_RAW_FIRST_QUESTION = "Сколько стоит 6 часов?"
_RAW_FOLLOWUP_QUESTION = "А скидка для группы есть?"
_OPERATOR_USERNAME = "@ops_demo"
_OPERATOR_CHAT_ID = "9999"


@pytest.fixture
def wired(tmp_path, monkeypatch) -> AsyncMock:
    """Wire fresh sqlite paths + park chat in pricing + stub telegram.

    Returns the ``AsyncMock`` that replaced ``telegram_bot_sender.send_message``
    so tests can inspect every outbound text the dispatcher emits — both
    the customer-facing fallback line AND the operator follow-up notify.
    """
    monkeypatch.setattr(api_main.settings, "app_env", "dev")

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

    # Park the chat in ``pricing`` with empty KB so EVERY inbound on
    # this chat routes to the price_unknown escalation branch.
    state_repo.upsert(
        chat_id=_CHAT_ID,
        project_id=_PROJECT_ID,
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

    send_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(
        api_main.telegram_bot_sender, "send_message", send_mock
    )
    monkeypatch.setattr(
        api_main.settings,
        "hitl_primary_operator_username",
        _OPERATOR_USERNAME,
    )
    monkeypatch.setattr(
        api_main.settings,
        "hitl_primary_operator_chat_id",
        _OPERATOR_CHAT_ID,
    )
    return send_mock


def test_second_sales_escalation_coalesces_onto_existing_ticket(
    wired, caplog
) -> None:
    """Two sales-escalation inbounds on the same chat → ONE HITL ticket,
    second response is ``coalesced=True``, operator gets a
    ``[follow-up]`` notify, and the second answer-trace carries the
    ``coalesced_sales_followup`` limitation."""
    send_mock = wired
    client = TestClient(api_app)

    # First inbound — creates the HITL ticket (price_unknown).
    first_response = client.post(
        "/conversations/inbound",
        json={
            "text": _RAW_FIRST_QUESTION,
            "chat_id": _CHAT_ID,
            "customer_username": "@danil",
            "trace_id": "trace-coalesce-first",
        },
    )
    assert first_response.status_code == 200, first_response.text
    first_body = first_response.json()
    assert first_body["escalated"] is True
    assert first_body.get("coalesced") is None  # first ticket, not a coalesce
    assert first_body["hitl_reason"] == "price_unknown"
    first_ticket_id = first_body["hitl_ticket_id"]
    assert isinstance(first_ticket_id, int)

    # Confirm the ticket is open/assigned so the second inbound finds it.
    active_after_first = api_main.hitl_ticket_repository.find_active_for_chat(
        _CHAT_ID
    )
    assert active_after_first is not None
    assert active_after_first.id == first_ticket_id

    sends_after_first = list(send_mock.await_args_list)

    with caplog.at_level("INFO"):
        # Second inbound on the same chat — must coalesce.
        second_response = client.post(
            "/conversations/inbound",
            json={
                "text": _RAW_FOLLOWUP_QUESTION,
                "chat_id": _CHAT_ID,
                "customer_username": "@danil",
                "trace_id": "trace-coalesce-second",
            },
        )
    assert second_response.status_code == 200, second_response.text
    second_body = second_response.json()

    # Coalesce branch contract.
    assert second_body["escalated"] is True
    assert second_body["coalesced"] is True
    assert second_body["hitl_ticket_id"] == first_ticket_id
    assert second_body["answer_text"] == PRICING_MISS_FALLBACK
    assert second_body["hitl_reason"] == "price_unknown"

    # Exactly one HITL ticket exists across both inbounds.
    all_tickets = api_main.hitl_ticket_repository.list_all()
    active_tickets = [
        t for t in all_tickets if t.status in {"open", "assigned"}
    ]
    assert len(active_tickets) == 1, (
        f"expected exactly one active ticket; saw {active_tickets!r}"
    )
    assert active_tickets[0].id == first_ticket_id

    # Operator received a [follow-up] notify carrying the second
    # customer question verbatim.
    new_sends = send_mock.await_args_list[len(sends_after_first) :]
    operator_sends = [
        call
        for call in new_sends
        if call.kwargs.get("chat_id") == int(_OPERATOR_CHAT_ID)
    ]
    assert operator_sends, (
        "expected an operator-facing send on the coalesce path; "
        f"got {new_sends!r}"
    )
    operator_text = operator_sends[-1].kwargs.get("text", "")
    assert "[follow-up]" in operator_text
    assert _RAW_FOLLOWUP_QUESTION in operator_text
    assert f"HITL ticket #{first_ticket_id}" in operator_text

    # Answer-trace for the coalesced turn carries the dedicated limitation.
    trace = api_main.answer_trace_repository.get_by_trace_id(
        "trace-coalesce-second"
    )
    assert "coalesced_sales_followup" in trace.limitations
    assert "awaiting_human_response" in trace.limitations
    assert trace.hitl_ticket_id == first_ticket_id

    # Structured log line emitted by the coalesce branch.
    assert any(
        record.message == "sales_escalation_coalesced"
        for record in caplog.records
    )
