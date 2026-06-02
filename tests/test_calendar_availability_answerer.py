from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.calendar.access_token_cache import CalendarReconnectNeeded
from services.api.app.calendar.availability_answerer import (
    RESPONSE_MODE_ANSWER,
    RESPONSE_MODE_ESCALATION,
    CalendarAvailabilityAnswerer,
)
from services.api.app.calendar.calendar_client import (
    BusyInterval,
    CalendarProviderError,
    FreeBusy,
)
from services.api.app.calendar.settings_repository import (
    CalendarProjectSettings,
    ServiceRule,
)
from services.api.app.calendar.token_repository import TokenNotFound
from services.api.app.russian_text import get_russian_normalizer

_OPERATOR = "@cal_op"
_OPERATOR_CHAT_ID = 9001


def _ctx(*, chat_id: int | None = 42, trace_id: str = "t-1") -> AnswerContext:
    return AnswerContext(
        chat_id=chat_id,
        customer_username="@cust",
        trace_id=trace_id,
        # A Friday afternoon, so a "в субботу" request resolves to a future day.
        now=datetime(2026, 5, 22, 9, 0, tzinfo=UTC),
        country_code="RU",
        timezone="Europe/Moscow",
        project_id=11,
    )


def _settings(
    *, enabled: bool = True, operator: str | None = _OPERATOR
) -> CalendarProjectSettings:
    return CalendarProjectSettings(
        project_id=11,
        enabled=enabled,
        calendar_operator=operator,
        project_timezone="Europe/Moscow",
        lookahead_days=60,
        updated_at=None,
    )


def _manicure_rule() -> ServiceRule:
    # Saturday is a service day; 10:00–19:00 working hours.
    return ServiceRule(
        id=1,
        project_id=11,
        name="маникюр",
        duration_minutes=60,
        working_hours={"sat": ["10:00", "19:00"]},
        service_days=["sat"],
        date_exceptions=None,
        updated_at=None,
    )


def _haircut_rule() -> ServiceRule:
    return ServiceRule(
        id=2,
        project_id=11,
        name="стрижка",
        duration_minutes=30,
        working_hours={"sat": ["10:00", "19:00"]},
        service_days=["sat"],
        date_exceptions=None,
        updated_at=None,
    )


class _FakeSettings:
    def __init__(
        self,
        *,
        enabled: bool = True,
        settings: CalendarProjectSettings | None = None,
        rules: list[ServiceRule] | None = None,
    ) -> None:
        self._enabled = enabled
        self._settings = settings if settings is not None else _settings()
        self._rules = rules if rules is not None else [_manicure_rule()]
        self.is_enabled_calls = 0
        self.get_calls = 0
        self.list_calls = 0

    def is_enabled(self, project_id: int) -> bool:
        self.is_enabled_calls += 1
        return self._enabled

    def get(self, project_id: int) -> CalendarProjectSettings | None:
        self.get_calls += 1
        return self._settings

    def list_service_rules(self, project_id: int) -> list[ServiceRule]:
        self.list_calls += 1
        return list(self._rules)


class _FakeClarify:
    def __init__(self, *, armed: bool = False) -> None:
        self._armed: dict[int, bool] = {}
        if armed:
            self._armed[42] = True
        self.arm_calls: list[tuple[int, str]] = []
        self.clear_calls: list[int] = []

    def is_armed(self, chat_id: int) -> bool:
        return self._armed.get(chat_id, False)

    def arm(self, chat_id: int, *, trace_id: str) -> None:
        self._armed[chat_id] = True
        self.arm_calls.append((chat_id, trace_id))

    def clear(self, chat_id: int) -> None:
        self._armed.pop(chat_id, None)
        self.clear_calls.append(chat_id)


def _free_busy(*intervals: BusyInterval) -> FreeBusy:
    return FreeBusy(calendar_id="primary", busy=tuple(intervals))


def _build(
    *,
    settings_repo: _FakeSettings,
    token_provider=None,
    freebusy_client=None,
    clarify: _FakeClarify | None = None,
    operator_chat_id: int | None = _OPERATOR_CHAT_ID,
) -> CalendarAvailabilityAnswerer:
    return CalendarAvailabilityAnswerer(
        settings_repo=settings_repo,
        token_provider=token_provider,
        freebusy_client=freebusy_client,
        normalizer=get_russian_normalizer(),
        clarify_store=clarify if clarify is not None else _FakeClarify(),
        operator_chat_resolver=lambda operator: operator_chat_id,
    )


def _provider(token: str = "tok") -> AsyncMock:
    provider = AsyncMock()
    provider.get_access_token = AsyncMock(return_value=token)
    return provider


def _freebusy(free_busy: FreeBusy) -> AsyncMock:
    client = AsyncMock()
    client.query_busy = AsyncMock(return_value=free_busy)
    return client


@pytest.mark.asyncio
async def test_no_project_id_skips() -> None:
    settings_repo = _FakeSettings()
    answerer = _build(settings_repo=settings_repo)
    result = await answerer.try_answer(
        question="можно записаться на маникюр в субботу в 15:00?",
        ctx=AnswerContext(
            chat_id=None,
            customer_username=None,
            trace_id="t",
            now=datetime(2026, 5, 22, 9, 0, tzinfo=UTC),
            project_id=None,
        ),
    )
    assert result.handled is False
    # No project -> the cheap gate is never even hit.
    assert settings_repo.is_enabled_calls == 0


@pytest.mark.asyncio
async def test_disabled_skips_without_intent_or_api_work() -> None:
    settings_repo = _FakeSettings(enabled=False)
    answerer = _build(settings_repo=settings_repo)
    result = await answerer.try_answer(
        question="можно записаться на маникюр в субботу в 15:00?",
        ctx=_ctx(),
    )
    assert result.handled is False
    # The cheap gate ran; NO settings.get / list_service_rules / API work beyond it.
    assert settings_repo.is_enabled_calls == 1
    assert settings_repo.get_calls == 0
    assert settings_repo.list_calls == 0


@pytest.mark.asyncio
async def test_non_scheduling_intent_skips() -> None:
    settings_repo = _FakeSettings()
    answerer = _build(settings_repo=settings_repo)
    result = await answerer.try_answer(
        question="какая у вас цена на услуги?",
        ctx=_ctx(),
    )
    assert result.handled is False
    # Gate passed but no service work happened (intent gate rejected first).
    assert settings_repo.get_calls == 0


@pytest.mark.asyncio
async def test_resolved_available_returns_russian_answer() -> None:
    settings_repo = _FakeSettings()
    clarify = _FakeClarify()
    answerer = _build(
        settings_repo=settings_repo,
        token_provider=_provider(),
        freebusy_client=_freebusy(_free_busy()),
        clarify=clarify,
    )
    result = await answerer.try_answer(
        question="можно записаться на маникюр в субботу в 15:00?",
        ctx=_ctx(),
    )
    assert result.handled is True
    assert result.response_mode == RESPONSE_MODE_ANSWER
    assert result.metadata["available"] is True
    assert "свободно" in (result.text or "")
    # A resolved request clears any stale clarify flag.
    assert clarify.clear_calls == [42]


@pytest.mark.asyncio
async def test_busy_slot_returns_not_available() -> None:
    # 2026-05-23 is a Saturday; busy 15:00–16:00 MSK == 12:00–13:00 UTC.
    busy = BusyInterval(
        start=datetime(2026, 5, 23, 12, 0, tzinfo=UTC),
        end=datetime(2026, 5, 23, 13, 0, tzinfo=UTC),
    )
    answerer = _build(
        settings_repo=_FakeSettings(),
        token_provider=_provider(),
        freebusy_client=_freebusy(_free_busy(busy)),
    )
    result = await answerer.try_answer(
        question="можно записаться на маникюр в субботу в 15:00?",
        ctx=_ctx(),
    )
    assert result.handled is True
    assert result.response_mode == RESPONSE_MODE_ANSWER
    assert result.metadata["available"] is False
    assert result.metadata["reason"] == "busy"
    assert "недоступно" in (result.text or "")


@pytest.mark.asyncio
async def test_out_of_rules_day_not_available() -> None:
    # Service runs only Mondays; a "в субботу" (Saturday) request is a wrong
    # service day even though the calendar is free.
    monday_only = ServiceRule(
        id=3,
        project_id=11,
        name="маникюр",
        duration_minutes=60,
        working_hours={"mon": ["10:00", "19:00"]},
        service_days=["mon"],
        date_exceptions=None,
        updated_at=None,
    )
    answerer = _build(
        settings_repo=_FakeSettings(rules=[monday_only]),
        token_provider=_provider(),
        freebusy_client=_freebusy(_free_busy()),
    )
    result = await answerer.try_answer(
        question="хочу записаться на маникюр в субботу в 15:00",
        ctx=_ctx(),
    )
    assert result.handled is True
    assert result.metadata["available"] is False
    assert result.metadata["reason"] == "wrong_service_day"


@pytest.mark.asyncio
async def test_no_match_first_turn_clarifies_then_escalates() -> None:
    clarify = _FakeClarify()
    answerer = _build(settings_repo=_FakeSettings(), clarify=clarify)
    # First turn: no configured service named -> ONE clarifying question.
    # NB: a NON-sales scheduling ask ("запишите меня …", no service word). A
    # sales-intent NoMatch now defers to the persona (Story 12.37 / D11), so this
    # legacy clarify→escalate path is exercised with a non-sales phrasing.
    first = await answerer.try_answer(
        question="запишите меня в субботу в 15:00",
        ctx=_ctx(),
    )
    assert first.handled is True
    assert first.response_mode == RESPONSE_MODE_ANSWER
    assert first.metadata["clarify"] is True
    assert clarify.arm_calls == [(42, "t-1")]

    # Second still-unresolved turn: escalate (no second clarification).
    second = await answerer.try_answer(
        question="запишите меня в субботу в 15:00",
        ctx=_ctx(trace_id="t-2"),
    )
    assert second.handled is True
    assert second.response_mode == RESPONSE_MODE_ESCALATION
    assert second.metadata["escalate"] is True
    assert second.metadata["reason"] == "service_no_match_after_clarify"


@pytest.mark.asyncio
async def test_sales_intent_no_match_defers_to_persona() -> None:
    """Story 12.37 (D11) — a sales-intent booking whose service this legacy
    resolver can't match must DEFER (skip) to the SalesPersonaAnswerer, never
    emit the "на какую услугу и на какое время…" clarify."""
    clarify = _FakeClarify()
    answerer = _build(settings_repo=_FakeSettings(), clarify=clarify)
    # Sales intent ("хочу записаться …") naming an UNCONFIGURED service ("покраску")
    # → NoMatch → defer to the persona (not the legacy clarify).
    result = await answerer.try_answer(
        question="хочу записаться на покраску в субботу в 15:00", ctx=_ctx()
    )
    assert result.handled is False  # ceded to the sales persona
    assert clarify.arm_calls == []  # no clarify armed


@pytest.mark.asyncio
async def test_ambiguous_first_turn_clarifies_with_options() -> None:
    settings_repo = _FakeSettings(rules=[_manicure_rule(), _haircut_rule()])
    answerer = _build(settings_repo=settings_repo)
    # Mentions both configured services -> Ambiguous -> clarify listing options.
    result = await answerer.try_answer(
        question="запишите на маникюр и стрижку в субботу в 15:00",
        ctx=_ctx(),
    )
    assert result.handled is True
    assert result.metadata["clarify"] is True
    assert result.metadata["reason"] == "service_ambiguous"
    assert "маникюр" in (result.text or "")
    assert "стрижка" in (result.text or "")


@pytest.mark.asyncio
async def test_ambiguous_after_clarify_escalates() -> None:
    settings_repo = _FakeSettings(rules=[_manicure_rule(), _haircut_rule()])
    answerer = _build(settings_repo=settings_repo, clarify=_FakeClarify(armed=True))
    result = await answerer.try_answer(
        question="запишите на маникюр и стрижку в субботу в 15:00",
        ctx=_ctx(),
    )
    assert result.response_mode == RESPONSE_MODE_ESCALATION
    assert result.metadata["reason"] == "service_ambiguous_after_clarify"


@pytest.mark.asyncio
async def test_no_time_clarifies_then_escalates() -> None:
    clarify = _FakeClarify()
    answerer = _build(settings_repo=_FakeSettings(), clarify=clarify)
    # Service resolves but no concrete day+time -> clarify once.
    first = await answerer.try_answer(
        question="хочу записаться на маникюр",
        ctx=_ctx(),
    )
    assert first.metadata["clarify"] is True
    assert first.metadata["reason"] == "no_requested_time"
    # Second turn still no time -> escalate.
    second = await answerer.try_answer(
        question="на маникюр запишите пожалуйста",
        ctx=_ctx(),
    )
    assert second.response_mode == RESPONSE_MODE_ESCALATION
    assert second.metadata["reason"] == "no_requested_time_after_clarify"


@pytest.mark.asyncio
async def test_clarify_without_chat_id_escalates_first_turn() -> None:
    # FR-22 contract requires tracking one clarifying turn; without a chat id we
    # cannot track that state, so the answerer must escalate immediately rather
    # than loop clarify on every inbound.
    clarify = _FakeClarify()
    answerer = _build(settings_repo=_FakeSettings(), clarify=clarify)
    result = await answerer.try_answer(
        question="хочу записаться на маникюр",
        ctx=AnswerContext(
            chat_id=None,
            customer_username=None,
            trace_id="t",
            now=datetime(2026, 5, 22, 9, 0, tzinfo=UTC),
            project_id=11,
        ),
    )
    assert result.handled is True
    assert result.response_mode == RESPONSE_MODE_ESCALATION
    assert result.metadata["escalate"] is True
    assert result.metadata["reason"] == "no_requested_time_no_chat_id"
    assert result.metadata["calendar_operator"] == _OPERATOR
    assert result.text is None
    # No state was armed (we don't have a chat id to key it on).
    assert clarify.arm_calls == []


@pytest.mark.asyncio
async def test_not_connected_no_provider_escalates() -> None:
    # token_provider/freebusy_client unwired (OAuth not configured) -> escalate.
    answerer = _build(settings_repo=_FakeSettings())
    result = await answerer.try_answer(
        question="можно записаться на маникюр в субботу в 15:00?",
        ctx=_ctx(),
    )
    assert result.response_mode == RESPONSE_MODE_ESCALATION
    assert result.metadata["reason"] == "calendar_not_connected"
    assert result.metadata["calendar_operator"] == _OPERATOR
    assert result.text is None


@pytest.mark.asyncio
async def test_not_connected_no_operator_escalates() -> None:
    settings_repo = _FakeSettings(settings=_settings(operator=None))
    answerer = _build(
        settings_repo=settings_repo,
        token_provider=_provider(),
        freebusy_client=_freebusy(_free_busy()),
    )
    result = await answerer.try_answer(
        question="можно записаться на маникюр в субботу в 15:00?",
        ctx=_ctx(),
    )
    assert result.response_mode == RESPONSE_MODE_ESCALATION
    assert result.metadata["reason"] == "calendar_not_connected"


@pytest.mark.asyncio
async def test_operator_chat_id_unknown_escalates() -> None:
    answerer = _build(
        settings_repo=_FakeSettings(),
        token_provider=_provider(),
        freebusy_client=_freebusy(_free_busy()),
        operator_chat_id=None,
    )
    result = await answerer.try_answer(
        question="можно записаться на маникюр в субботу в 15:00?",
        ctx=_ctx(),
    )
    assert result.response_mode == RESPONSE_MODE_ESCALATION
    assert result.metadata["reason"] == "operator_chat_id_unknown"


@pytest.mark.asyncio
async def test_reconnect_needed_escalates_to_operator() -> None:
    provider = AsyncMock()
    provider.get_access_token = AsyncMock(side_effect=CalendarReconnectNeeded("x"))
    answerer = _build(
        settings_repo=_FakeSettings(),
        token_provider=provider,
        freebusy_client=_freebusy(_free_busy()),
    )
    result = await answerer.try_answer(
        question="можно записаться на маникюр в субботу в 15:00?",
        ctx=_ctx(),
    )
    assert result.response_mode == RESPONSE_MODE_ESCALATION
    assert result.metadata["reason"] == "reconnect_needed"
    assert result.metadata["calendar_operator"] == _OPERATOR
    assert result.text is None


@pytest.mark.asyncio
async def test_token_not_found_escalates() -> None:
    provider = AsyncMock()
    provider.get_access_token = AsyncMock(side_effect=TokenNotFound("x"))
    answerer = _build(
        settings_repo=_FakeSettings(),
        token_provider=provider,
        freebusy_client=_freebusy(_free_busy()),
    )
    result = await answerer.try_answer(
        question="можно записаться на маникюр в субботу в 15:00?",
        ctx=_ctx(),
    )
    assert result.response_mode == RESPONSE_MODE_ESCALATION
    assert result.metadata["reason"] == "token_not_found"


@pytest.mark.asyncio
async def test_provider_error_escalates_no_fabricated_answer() -> None:
    client = AsyncMock()
    client.query_busy = AsyncMock(side_effect=CalendarProviderError("down"))
    answerer = _build(
        settings_repo=_FakeSettings(),
        token_provider=_provider(),
        freebusy_client=client,
    )
    result = await answerer.try_answer(
        question="можно записаться на маникюр в субботу в 15:00?",
        ctx=_ctx(),
    )
    assert result.response_mode == RESPONSE_MODE_ESCALATION
    assert result.metadata["reason"] == "provider_error"
    assert result.text is None
