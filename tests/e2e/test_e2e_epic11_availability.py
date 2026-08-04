"""Epic 11 / story 11.07 — availability answerer end-to-end.

Enables a project + connects an operator (Google mocked), then drives
``/conversations/inbound`` through the assembled pipeline: a free time →
Russian "available"; a busy time → "not available"; an ambiguous service →
clarify then escalate; a provider error → escalate routed to the calendar
operator. The calendar answerer sits before ``grounded_rag``, so an answered
availability question never reaches RAG.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.answerers import AnswerPipeline
from services.api.app.calendar.availability_answerer import (
    CalendarAvailabilityAnswerer,
)
from services.api.app.calendar.calendar_client import (
    BusyInterval,
    CalendarProviderError,
    FreeBusy,
)
from services.api.app.calendar.clarify_state_repository import (
    CalendarClarifyStateRepository,
)
from services.api.app.calendar.settings_repository import CalendarSettingsRepository
from services.api.app.main import app as api_app
from services.api.app.russian_text import get_russian_normalizer

pytestmark = [pytest.mark.e2e, pytest.mark.epic("11"), pytest.mark.story("11-07")]

_PROJECT_ID = 1  # the default project
_OPERATOR = "@cal_op"
_OPERATOR_CHAT_ID = 7777
_CHAT_ID = 9001
# A Friday morning, so "в субботу" resolves to the next day (a future slot).
_NOW = datetime(2026, 5, 22, 6, 0, tzinfo=UTC)


class _FrozenClock:
    def now(self) -> datetime:
        return _NOW


def _build_pipeline(
    *,
    settings_repo: CalendarSettingsRepository,
    clarify: CalendarClarifyStateRepository,
    token_provider: Any,
    freebusy_client: Any,
) -> AnswerPipeline:
    calendar = CalendarAvailabilityAnswerer(
        settings_repo=settings_repo,
        token_provider=token_provider,
        freebusy_client=freebusy_client,
        normalizer=get_russian_normalizer(),
        clarify_store=clarify,
        operator_chat_resolver=lambda operator: _OPERATOR_CHAT_ID,
    )

    class _NeverRag:
        name = "grounded_rag"

        async def try_answer(self, *, question, ctx):
            from services.api.app.answerers import AnswerResult

            return AnswerResult(handled=False)

    return AnswerPipeline([calendar, _NeverRag()])


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    calendar_db = str(tmp_path / "calendar.sqlite3")
    settings_repo = CalendarSettingsRepository(db_path=calendar_db)
    settings_repo.enable(_PROJECT_ID, calendar_operator=_OPERATOR)
    # маникюр: Saturdays 10:00–19:00, 60 min.
    settings_repo.upsert_service_rule(
        project_id=_PROJECT_ID,
        name="маникюр",
        duration_minutes=60,
        working_hours={"sat": ["10:00", "19:00"]},
        service_days=["sat"],
    )
    settings_repo.upsert_service_rule(
        project_id=_PROJECT_ID,
        name="стрижка",
        duration_minutes=30,
        working_hours={"sat": ["10:00", "19:00"]},
        service_days=["sat"],
    )
    clarify = CalendarClarifyStateRepository(db_path=str(tmp_path / "clarify.sqlite3"))

    api_main.hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    api_main.incident_repository.db_path = str(tmp_path / "incidents.sqlite3")
    api_main.answer_trace_repository.db_path = str(tmp_path / "traces.sqlite3")
    monkeypatch.setattr(
        api_main.telegram_bot_sender, "send_message", AsyncMock(return_value=1)
    )
    monkeypatch.setattr(api_main, "_resolve_inbound_project_id", lambda chat_id: _PROJECT_ID)
    monkeypatch.setattr(
        api_main, "calendar_settings_repository", settings_repo
    )

    # Freeze the answer clock so "в субботу" resolves to a deterministic future
    # Saturday regardless of the wall clock the suite runs on.
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

    state: dict[str, Any] = {
        "settings_repo": settings_repo,
        "clarify": clarify,
        "token_provider": _make_token_provider(),
        "freebusy_client": _make_freebusy(FreeBusy(calendar_id="primary")),
    }

    def _install() -> None:
        monkeypatch.setattr(
            api_main,
            "answer_pipeline",
            _build_pipeline(
                settings_repo=settings_repo,
                clarify=clarify,
                token_provider=state["token_provider"],
                freebusy_client=state["freebusy_client"],
            ),
        )

    state["install"] = _install
    state["client"] = TestClient(api_app)
    yield state


def _make_token_provider() -> AsyncMock:
    provider = AsyncMock()
    provider.get_access_token = AsyncMock(return_value="access-token")
    return provider


def _make_freebusy(free_busy: FreeBusy) -> AsyncMock:
    client = AsyncMock()
    client.query_busy = AsyncMock(return_value=free_busy)
    return client


def _inbound(client: TestClient, *, text: str, trace_id: str) -> dict[str, Any]:
    return client.post(
        "/conversations/inbound",
        json={"text": text, "chat_id": _CHAT_ID, "trace_id": trace_id},
    ).json()


def test_e2e_available_slot_returns_russian_available(env) -> None:
    env["install"]()
    body = _inbound(
        env["client"],
        text="можно записаться на маникюр в субботу в 15:00?",
        trace_id="t-avail",
    )
    assert body["response_mode"] == "calendar_availability"
    assert body["escalated"] is False
    assert "свободно" in body["answer_text"]
    assert body["answerer"] == "calendar_availability"


def test_e2e_customer_can_compare_two_bookable_slots_before_commitment(env) -> None:
    """A customer can ask when to book and compare another slot in the same chat."""
    env["install"]()
    first = _inbound(
        env["client"],
        text="Подскажите, когда можно записаться на маникюр в субботу в 15:00?",
        trace_id="t-compare-1",
    )
    second = _inbound(
        env["client"],
        text="А на маникюр в субботу в 16:00 можно записаться?",
        trace_id="t-compare-2",
    )

    assert first["response_mode"] == "calendar_availability"
    assert first["escalated"] is False
    assert "свободно" in first["answer_text"]
    assert second["response_mode"] == "calendar_availability"
    assert second["escalated"] is False
    assert "свободно" in second["answer_text"]
    env["freebusy_client"].query_busy.assert_awaited()
    assert env["freebusy_client"].query_busy.await_count == 2


def test_e2e_busy_slot_returns_not_available(env) -> None:
    # Busy 15:00–16:00 MSK on Saturday == 12:00–13:00 UTC.
    busy = BusyInterval(
        start=datetime(2026, 5, 23, 12, 0, tzinfo=UTC),
        end=datetime(2026, 5, 23, 13, 0, tzinfo=UTC),
    )
    env["freebusy_client"] = _make_freebusy(
        FreeBusy(calendar_id="primary", busy=(busy,))
    )
    env["install"]()
    body = _inbound(
        env["client"],
        text="можно записаться на маникюр в субботу в 15:00?",
        trace_id="t-busy",
    )
    assert body["response_mode"] == "calendar_availability"
    assert "недоступно" in body["answer_text"]


def test_e2e_ambiguous_service_clarifies_then_escalates(env) -> None:
    env["install"]()
    client = env["client"]
    first = _inbound(
        client,
        text="запишите на маникюр и стрижку в субботу в 15:00",
        trace_id="t-amb-1",
    )
    assert first["response_mode"] == "calendar_availability"
    assert "маникюр" in first["answer_text"]
    # Second still-ambiguous turn escalates to the calendar operator.
    second = _inbound(
        client,
        text="ну на маникюр и стрижку же, в субботу в 15:00",
        trace_id="t-amb-2",
    )
    assert second["escalated"] is True
    assert second["response_mode"] == "human_only"
    assert second["hitl_ticket_id"] is not None


def test_e2e_provider_error_escalates_to_calendar_operator(env) -> None:
    client_mock = AsyncMock()
    client_mock.query_busy = AsyncMock(side_effect=CalendarProviderError("down"))
    env["freebusy_client"] = client_mock
    env["install"]()
    body = _inbound(
        env["client"],
        text="можно записаться на маникюр в субботу в 15:00?",
        trace_id="t-provider",
    )
    assert body["escalated"] is True
    assert body["response_mode"] == "human_only"
    # No fabricated availability answer reached the customer.
    assert "answer_text" not in body or body.get("answer_text") is None

    tickets = env["client"].get("/hitl/tickets").json()["items"]
    assert len(tickets) == 1
    assert tickets[0]["target_chat_id"] == _CHAT_ID


def test_e2e_token_provider_window_is_single_freebusy_call(env) -> None:
    env["install"]()
    _inbound(
        env["client"],
        text="можно записаться на маникюр в субботу в 15:00?",
        trace_id="t-single",
    )
    freebusy = env["freebusy_client"]
    freebusy.query_busy.assert_awaited_once()
    kwargs = freebusy.query_busy.await_args.kwargs
    # One window-wide call (not per-slot): time_max == now + lookahead (60d).
    assert kwargs["time_max"] - kwargs["time_min"] == timedelta(days=60)
