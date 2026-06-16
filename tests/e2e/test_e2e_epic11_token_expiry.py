"""Epic 11 / story 11.07 (deferred from 11.04) — token-expiry end-to-end.

When an operator's refresh token is revoked/expired, the *next* availability
request that needs it must: move the operator to ``reconnect_needed`` + delete
the poison token, emit an incident, DM the operator to reconnect, and escalate
the customer to a human (never a fabricated answer). Drives the real
``AccessTokenProvider`` (failing refresh) through ``/conversations/inbound``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.answerers import AnswerPipeline, AnswerResult
from services.api.app.calendar.access_token_cache import AccessTokenProvider
from services.api.app.calendar.availability_answerer import (
    CalendarAvailabilityAnswerer,
)
from services.api.app.calendar.clarify_state_repository import (
    CalendarClarifyStateRepository,
)
from services.api.app.calendar.oauth import TokenRefreshFailed
from services.api.app.calendar.settings_repository import CalendarSettingsRepository
from services.api.app.calendar.token_repository import (
    STATUS_RECONNECT_NEEDED,
    CalendarTokenRepository,
)
from services.api.app.main import app as api_app
from services.api.app.russian_text import get_russian_normalizer

pytestmark = [pytest.mark.e2e, pytest.mark.epic("11"), pytest.mark.story("11-07")]

_PROJECT_ID = 1
_OPERATOR = "@cal_op"
_OPERATOR_CHAT_ID = 7777
_CHAT_ID = 9001
_NOW = datetime(2026, 5, 22, 6, 0, tzinfo=UTC)


class _FrozenClock:
    def now(self) -> datetime:
        return _NOW


class _FailingOAuthClient:
    """A refresh that always reports the stored refresh token is dead."""

    def refresh(self, *, refresh_token: str):
        raise TokenRefreshFailed("refresh_failed")


class _NeverRag:
    name = "grounded_rag"

    async def try_answer(self, *, question, ctx) -> AnswerResult:
        return AnswerResult(handled=False)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    calendar_db = str(tmp_path / "calendar.sqlite3")
    settings_repo = CalendarSettingsRepository(db_path=calendar_db)
    settings_repo.enable(_PROJECT_ID, calendar_operator=_OPERATOR)
    settings_repo.upsert_service_rule(
        project_id=_PROJECT_ID,
        name="маникюр",
        duration_minutes=60,
        working_hours={"sat": ["10:00", "19:00"]},
        service_days=["sat"],
    )
    token_repo = CalendarTokenRepository(
        db_path=calendar_db, fernet=Fernet(Fernet.generate_key())
    )
    # A stored-but-dead token: present, so we get past TokenNotFound and into
    # the refresh that fails with TokenRefreshFailed.
    token_repo.upsert(_PROJECT_ID, _OPERATOR, "dead-refresh-token")

    api_main.hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    api_main.incident_repository.db_path = str(tmp_path / "incidents.sqlite3")
    api_main.answer_trace_repository.db_path = str(tmp_path / "traces.sqlite3")
    send_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(api_main.telegram_bot_sender, "send_message", send_mock)
    monkeypatch.setattr(
        api_main, "_resolve_inbound_project_id", lambda chat_id: _PROJECT_ID
    )

    _real_build_ctx = api_main._build_answer_context

    def _frozen_build_ctx(
        *,
        chat_id,
        customer_username,
        trace_id,
        now,
        delivery_channel="bot",
        operator_id=None,
    ):
        return _real_build_ctx(
            chat_id=chat_id,
            customer_username=customer_username,
            trace_id=trace_id,
            now=_NOW,
            delivery_channel=delivery_channel,
            operator_id=operator_id,
        )

    monkeypatch.setattr(api_main, "_build_answer_context", _frozen_build_ctx)

    token_provider = AccessTokenProvider(
        oauth_client=_FailingOAuthClient(),
        token_repo=token_repo,
        clock=_FrozenClock(),
        lock_factory=__import__("asyncio").Lock,
        incident_sink=api_main.incident_repository,
        notifier=api_main.telegram_bot_sender,
    )
    calendar = CalendarAvailabilityAnswerer(
        settings_repo=settings_repo,
        token_provider=token_provider,
        freebusy_client=AsyncMock(),
        normalizer=get_russian_normalizer(),
        clarify_store=CalendarClarifyStateRepository(
            db_path=str(tmp_path / "clarify.sqlite3")
        ),
        operator_chat_resolver=lambda operator: _OPERATOR_CHAT_ID,
    )
    monkeypatch.setattr(
        api_main, "answer_pipeline", AnswerPipeline([calendar, _NeverRag()])
    )

    client = TestClient(api_app)
    yield {
        "client": client,
        "token_repo": token_repo,
        "send_mock": send_mock,
    }


def test_e2e_expired_token_reconnect_incident_and_customer_escalates(env) -> None:
    client = env["client"]
    token_repo = env["token_repo"]

    body = client.post(
        "/conversations/inbound",
        json={
            "text": "можно записаться на маникюр в субботу в 15:00?",
            "chat_id": _CHAT_ID,
            "trace_id": "t-token-expiry",
        },
    ).json()

    # Customer escalates to a human — never a fabricated availability answer.
    assert body["escalated"] is True
    assert body["response_mode"] == "human_only"
    assert body.get("answer_text") is None

    # The poison token row is PRESERVED with status=reconnect_needed so the
    # reconnect DM is dedup'd persistently across api restarts (the spam fix —
    # see access_token_cache._handle_dead_token). The operator is "marked
    # dead" by status, not by deletion; the row is overwritten back to
    # status=connected only by a successful /connect_calendar upsert.
    assert (
        token_repo.get_status(_PROJECT_ID, _OPERATOR)
        == STATUS_RECONNECT_NEEDED
    )

    # An incident was emitted for the reconnect.
    incidents = client.get("/incidents").json()["items"]
    assert any(
        i["fingerprint"]
        == f"calendar_reconnect_needed:{_PROJECT_ID}:{_OPERATOR}"
        for i in incidents
    )

    # Both the operator reconnect DM and the customer ack/notify went out.
    sent_texts = [c.kwargs.get("text", "") for c in env["send_mock"].await_args_list]
    assert any("переподключите" in t for t in sent_texts)

    # The HITL ticket exists, targeting the customer chat.
    tickets = client.get("/hitl/tickets").json()["items"]
    assert len(tickets) == 1
    assert tickets[0]["target_chat_id"] == _CHAT_ID


def test_e2e_status_marked_reconnect_needed_constant_used(env) -> None:
    # Guard the status constant identity (revoked token -> reconnect_needed),
    # in case the reconnect flow is refactored.
    assert STATUS_RECONNECT_NEEDED == "reconnect_needed"
