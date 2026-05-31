"""Regression: a claimed inbound trace must never be silenced by a failure
that happens AFTER the Story 12.24 idempotency claim but BEFORE the pipeline.

Live symptom (báгги, Artur Yaskevich): the bot did not answer
"хочу забронировать багги завтра в 14:00" at all.

The window between ``claim_inbound`` (a PERMANENT row) and the pipeline's
own ``try/except`` runs ``maybe_cancel`` (follow-up cancel — real work for a
returning customer), ``_build_answer_context``, and ``_should_send_interim``
(a synchronous ``sales_state_repository.get`` read). If any of those raises
(e.g. a transient ``sqlite3.OperationalError: database is locked`` under
concurrent load), the request 500s with the claim already durable. The
bot_gateway then retries the SAME ``trace_id``; ``claim_inbound`` returns
False, so the retry is deduplicated with ``delivered=False`` and NO ack —
the customer is permanently silenced.

The codebase already guarantees "pipeline exception → ack, not silence"
(test_inbound_interim_ack.py::test_pipeline_exception_produces_hitl_ack_not_silence).
This test extends that invariant to the claim-window setup code.
"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from services.api.app.answerers import AnswerResult
from services.api.app.main import (
    answer_pipeline,
    answer_trace_repository,
    hitl_ticket_repository,
    incident_repository,
    rag_repository,
    sales_followup_repository,
    sales_state_repository,
    settings,
    telegram_bot_sender,
)
from services.api.app.main import app as api_app


def _wire(tmp_path) -> None:
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    incident_repository.db_path = str(tmp_path / "incidents.sqlite3")
    rag_repository.db_path = str(tmp_path / "rag.sqlite3")
    answer_trace_repository.db_path = str(tmp_path / "answer_traces.sqlite3")
    sales_state_repository.db_path = str(tmp_path / "sales.sqlite3")


def test_setup_failure_after_claim_does_not_silence_customer(tmp_path, monkeypatch):
    """A transient failure in the claim-window setup still delivers an ack.

    Before the fix the request 500s after claiming → the customer gets
    nothing, and the gateway's same-trace retry is deduped into silence.
    """
    _wire(tmp_path)
    sends: list[tuple[int, str]] = []

    async def _fake_send(*, chat_id: int, text: str) -> int:
        sends.append((chat_id, text))
        return len(sends)

    monkeypatch.setattr(telegram_bot_sender, "send_message", _fake_send)
    monkeypatch.setattr(settings, "inbound_ack_message", "Одну секунду, уточню.")

    # The pipeline WOULD deliver a real answer if it were reached.
    async def _ok_run(**_kwargs):
        return AnswerResult(handled=True, text="Готово!", response_mode="sales_booking")

    monkeypatch.setattr(answer_pipeline, "run", _ok_run)

    # Simulate a transient DB hiccup during the claim-window setup: the
    # synchronous state read inside _should_send_interim raises.
    def _boom(chat_id):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sales_state_repository, "get", _boom)

    client = TestClient(api_app, raise_server_exceptions=False)
    resp = client.post(
        "/conversations/inbound",
        json={
            "text": "хочу забронировать багги завтра в 14:00",
            "chat_id": 555,
            "customer_username": "@artur",
            "trace_id": "tg-update-claim-silence",
        },
    )

    # The request must not 500 (which the gateway would treat as a failed
    # forward and retry into a deduped silence)...
    assert resp.status_code == 200, resp.text
    # ...and the customer MUST receive something — never silence.
    assert sends, "customer was silenced: claim taken but no message delivered"


def test_retry_after_claim_window_failure_still_delivers(tmp_path, monkeypatch):
    """End-to-end of the gateway's retry: first attempt fails in the claim
    window, the gateway retries the SAME trace_id; the customer must still be
    answered exactly once rather than deduped into permanent silence."""
    _wire(tmp_path)
    sends: list[tuple[int, str]] = []

    async def _fake_send(*, chat_id: int, text: str) -> int:
        sends.append((chat_id, text))
        return len(sends)

    monkeypatch.setattr(telegram_bot_sender, "send_message", _fake_send)
    monkeypatch.setattr(settings, "inbound_ack_message", "Одну секунду, уточню.")

    async def _ok_run(**_kwargs):
        return AnswerResult(handled=True, text="Готово!", response_mode="sales_booking")

    monkeypatch.setattr(answer_pipeline, "run", _ok_run)

    client = TestClient(api_app, raise_server_exceptions=False)
    body = {
        "text": "хочу забронировать багги завтра в 14:00",
        "chat_id": 556,
        "customer_username": "@artur",
        "trace_id": "tg-update-claim-retry",
    }

    # First attempt fails in the claim window (transient DB error).
    def _boom(chat_id):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sales_state_repository, "get", _boom)
    client.post("/conversations/inbound", json=body)

    # The transient error clears; the gateway retries the same trace_id.
    monkeypatch.setattr(sales_state_repository, "get", lambda chat_id: None)
    client.post("/conversations/inbound", json=body)

    assert sends, "customer permanently silenced across the gateway retry"


def test_failure_before_context_built_still_acks(tmp_path, monkeypatch):
    """A failure in the EARLIEST claim-window step (the follow-up cancel, before
    the answer context is built) must still ack — exercises the ``ctx is None``
    fallback in the ack-message resolution so it never NameErrors into silence."""
    _wire(tmp_path)
    sends: list[tuple[int, str]] = []

    async def _fake_send(*, chat_id: int, text: str) -> int:
        sends.append((chat_id, text))
        return len(sends)

    monkeypatch.setattr(telegram_bot_sender, "send_message", _fake_send)
    monkeypatch.setattr(settings, "inbound_ack_message", "Одну секунду, уточню.")

    # maybe_cancel runs BEFORE _build_answer_context — make its repo call raise
    # so ctx is never assigned when the except handler resolves the ack message.
    def _boom(chat_id, *, now):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sales_followup_repository, "mark_cancelled_replied", _boom)

    client = TestClient(api_app, raise_server_exceptions=False)
    resp = client.post(
        "/conversations/inbound",
        json={
            "text": "хочу забронировать багги завтра в 14:00",
            "chat_id": 557,
            "customer_username": "@artur",
            "trace_id": "tg-update-claim-precontext",
        },
    )

    assert resp.status_code == 200, resp.text
    assert sends, "customer silenced when the pre-context step failed"
    assert sends[0] == (557, "Одну секунду, уточню.")
