"""Unit coverage for the calendar wiring helpers in ``services.api.app.main``
added by story 11.07: singleton builders, the operator-chat resolver, the
escalation assignee resolver, and the calendar-escalation HITL path (including
the coalesce-onto-active-ticket branch).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

from services.api.app import main as api_main
from services.api.app.calendar.access_token_cache import AccessTokenProvider
from services.api.app.calendar.calendar_client import CalendarFreeBusyClient
from services.api.app.calendar.oauth import CalendarOAuthClient
from services.api.app.calendar.token_repository import CalendarTokenRepository
from services.api.app.main import InboundMessageRequest


def test_system_clock_now_is_tz_aware() -> None:
    now = api_main._SystemClock().now()
    assert now.tzinfo is not None


def test_startup_bootstraps_services_nl_op_sessions_table() -> None:
    """Epic 13 (story 13.01): the api startup hook creates
    services_nl_op_sessions in semantaix_nl_ops.db alongside admin_nl_op_sessions.
    """
    db_path = api_main.settings.nl_ops_db_path
    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "services_nl_op_sessions" in tables


def test_startup_bootstraps_calendar_service_alias_hint_sent_table() -> None:
    """Epic 13 (story 13.03): the api startup hook creates the dedup table for
    the `/calendar_service` migration-hint DM in the same semantaix_nl_ops.db.
    """
    db_path = api_main.settings.nl_ops_db_path
    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "calendar_service_alias_hint_sent" in tables


def test_build_token_provider_returns_none_without_oauth(monkeypatch) -> None:
    monkeypatch.setattr(api_main, "calendar_oauth_client", None)
    monkeypatch.setattr(api_main, "calendar_token_repository", None)
    assert api_main._build_calendar_token_provider() is None


@pytest.mark.asyncio
async def test_startup_backfills_calendar_claims_without_sending_dm(
    monkeypatch, tmp_path: Path
) -> None:
    token_repo = CalendarTokenRepository(
        db_path=str(tmp_path / "calendar.sqlite3"),
        fernet=Fernet(Fernet.generate_key()),
    )
    token_repo.upsert(1, "@op", "token")
    monkeypatch.setattr(api_main, "calendar_token_repository", token_repo)

    await api_main.backfill_calendar_connection_notification_claims_on_startup()

    assert token_repo.claim_connection_notification(1, "@op") is False


@pytest.mark.asyncio
async def test_startup_calendar_claim_backfill_skips_when_not_configured(
    monkeypatch,
) -> None:
    monkeypatch.setattr(api_main, "calendar_token_repository", None)

    await api_main.backfill_calendar_connection_notification_claims_on_startup()


def test_build_token_provider_constructs_when_configured(
    monkeypatch, tmp_path: Path
) -> None:
    oauth = CalendarOAuthClient(
        client_id="cid", client_secret="sec", redirect_uri="https://x/cb"
    )
    token_repo = CalendarTokenRepository(
        db_path=str(tmp_path / "c.sqlite3"), fernet=Fernet(Fernet.generate_key())
    )
    monkeypatch.setattr(api_main, "calendar_oauth_client", oauth)
    monkeypatch.setattr(api_main, "calendar_token_repository", token_repo)
    provider = api_main._build_calendar_token_provider()
    assert isinstance(provider, AccessTokenProvider)


def test_build_freebusy_returns_none_without_oauth(monkeypatch) -> None:
    monkeypatch.setattr(api_main, "calendar_oauth_client", None)
    assert api_main._build_calendar_freebusy_client() is None


def test_build_freebusy_constructs_when_configured(monkeypatch) -> None:
    oauth = CalendarOAuthClient(
        client_id="cid", client_secret="sec", redirect_uri="https://x/cb"
    )
    monkeypatch.setattr(api_main, "calendar_oauth_client", oauth)
    client = api_main._build_calendar_freebusy_client()
    assert isinstance(client, CalendarFreeBusyClient)


def test_resolve_operator_chat_id_returns_none_when_unknown(monkeypatch) -> None:
    monkeypatch.setattr(
        api_main.operator_repository, "find_by_username", lambda username: None
    )
    assert api_main._resolve_calendar_operator_chat_id("@nobody") is None


def test_resolve_operator_chat_id_returns_chat_id(monkeypatch) -> None:
    class _Op:
        chat_id = 4242

    monkeypatch.setattr(
        api_main.operator_repository, "find_by_username", lambda username: _Op()
    )
    assert api_main._resolve_calendar_operator_chat_id("@cal_op") == 4242


def test_escalation_assignee_falls_back_to_primary_without_operator(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        api_main, "_effective_hitl_operator_username", lambda: "@primary"
    )
    assert api_main._resolve_calendar_escalation_assignee(None) == "@primary"


def test_escalation_assignee_falls_back_when_operator_inactive(monkeypatch) -> None:
    class _Op:
        username = "@cal_op"
        is_active = False

    monkeypatch.setattr(
        api_main, "_effective_hitl_operator_username", lambda: "@primary"
    )
    monkeypatch.setattr(
        api_main.operator_repository, "find_by_username", lambda username: _Op()
    )
    assert api_main._resolve_calendar_escalation_assignee("@cal_op") == "@primary"


def test_escalation_assignee_routes_to_active_calendar_operator(monkeypatch) -> None:
    class _Op:
        username = "@cal_op"
        is_active = True

    monkeypatch.setattr(
        api_main, "_effective_hitl_operator_username", lambda: "@primary"
    )
    monkeypatch.setattr(
        api_main.operator_repository, "find_by_username", lambda username: _Op()
    )
    assert api_main._resolve_calendar_escalation_assignee("@cal_op") == "@cal_op"


@pytest.mark.asyncio
async def test_calendar_escalation_coalesces_onto_active_ticket(
    monkeypatch, tmp_path: Path
) -> None:
    api_main.hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    api_main.answer_trace_repository.db_path = str(tmp_path / "traces.sqlite3")
    api_main.incident_repository.db_path = str(tmp_path / "inc.sqlite3")
    monkeypatch.setattr(
        api_main.telegram_bot_sender, "send_message", AsyncMock(return_value=1)
    )

    # Pre-create an active ticket for this chat so the escalation coalesces.
    existing = api_main.hitl_ticket_repository.create(
        conversation_ref="earlier question",
        reason="awaiting_human_response",
        target_chat_id=5555,
    )
    api_main.hitl_ticket_repository.assign(
        ticket_id=existing.id, operator_username="@cal_op"
    )

    notify = AsyncMock(return_value=True)
    monkeypatch.setattr(api_main, "_notify_hitl_operator_with_question", notify)

    request = InboundMessageRequest(
        text="можно записаться на маникюр в субботу в 15:00?",
        chat_id=5555,
        trace_id="t-coalesce",
    )
    result = await api_main._escalate_calendar_availability(
        request=request,
        trace_id="t-coalesce",
        latency_ms=5,
        metadata={
            "calendar_operator": "@cal_op",
            "escalation_context": "availability question; calendar error/uncertainty",
            "reason": "provider_error",
        },
    )
    assert result["coalesced"] is True
    assert result["escalated"] is True
    assert result["hitl_ticket_id"] == existing.id
    assert result["hitl_operator_username"] == "@cal_op"
    # The follow-up was forwarded with the context prefix.
    forwarded = notify.await_args.kwargs["question"]
    assert forwarded.startswith("[follow-up] [availability question")


@pytest.mark.asyncio
async def test_calendar_escalation_without_context_prefix(
    monkeypatch, tmp_path: Path
) -> None:
    api_main.hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    api_main.answer_trace_repository.db_path = str(tmp_path / "traces.sqlite3")
    api_main.incident_repository.db_path = str(tmp_path / "inc.sqlite3")
    monkeypatch.setattr(
        api_main.telegram_bot_sender, "send_message", AsyncMock(return_value=1)
    )
    monkeypatch.setattr(
        api_main, "_resolve_inbound_project_id", lambda chat_id: 1
    )
    monkeypatch.setattr(
        api_main, "_effective_hitl_operator_username", lambda: "@primary"
    )
    notify = AsyncMock(return_value=True)
    monkeypatch.setattr(api_main, "_notify_hitl_operator_with_question", notify)

    request = InboundMessageRequest(
        text="свободно ли в субботу?", chat_id=None, trace_id="t-nocontext"
    )
    result = await api_main._escalate_calendar_availability(
        request=request,
        trace_id="t-nocontext",
        latency_ms=3,
        metadata={"calendar_operator": None, "reason": "calendar_not_connected"},
    )
    assert result["escalated"] is True
    assert result["hitl_operator_username"] == "@primary"
    # No context -> the question is forwarded verbatim (no prefix).
    assert notify.await_args.kwargs["question"] == "свободно ли в субботу?"
