"""Operator NL "set booking hours" (schedule) action.

Lets an operator configure a service's bookable schedule by talking to the bot
("работаем с 8 до 21 каждый день") instead of the ``/service edit`` slash
syntax. The action routes through the SAME canonical project-services upsert the
``/service`` command uses (so the availability engine reads the result), with an
in-place edit that preserves the service's description/price/tags.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from services.bot_gateway.app.operator_service_nl import (
    ServiceIntent,
    classify_service_intent,
    handle_operator_service_nl_message,
    parse_schedule,
)
from services.bot_gateway.app.telegram_update import NormalizedTelegramMessage

# --- classifier -------------------------------------------------------------


class _FakeOpenRouter:
    def __init__(self, payload: dict) -> None:
        self.complete_json = AsyncMock(return_value=payload)


@pytest.mark.asyncio
async def test_classifier_returns_schedule_with_all_fields() -> None:
    fake = _FakeOpenRouter(
        {
            "action": "schedule",
            "name": "аренда багги",
            "hours": "08:00-21:00",
            "days": "mon-sun",
            "duration_minutes": 60,
        }
    )
    intent = await classify_service_intent(
        "поставь часы работы аренда багги с 8 до 21", openrouter=fake
    )
    assert intent == ServiceIntent(
        action="schedule",
        name="аренда багги",
        description=None,
        hours="08:00-21:00",
        days="mon-sun",
        duration_minutes=60,
    )


@pytest.mark.asyncio
async def test_classifier_schedule_missing_name_falls_through() -> None:
    fake = _FakeOpenRouter(
        {"action": "schedule", "name": None, "hours": "08:00-21:00"}
    )
    assert await classify_service_intent("часы с 8 до 21", openrouter=fake) is None


@pytest.mark.asyncio
async def test_classifier_schedule_bad_duration_type_falls_through() -> None:
    fake = _FakeOpenRouter(
        {"action": "schedule", "name": "багги", "duration_minutes": "60"}
    )
    assert await classify_service_intent("часы багги", openrouter=fake) is None


@pytest.mark.asyncio
async def test_classifier_schedule_bad_hours_type_falls_through() -> None:
    fake = _FakeOpenRouter({"action": "schedule", "name": "багги", "hours": 800})
    assert await classify_service_intent("часы багги", openrouter=fake) is None


@pytest.mark.asyncio
async def test_classifier_schedule_bad_days_type_falls_through() -> None:
    fake = _FakeOpenRouter({"action": "schedule", "name": "багги", "days": ["mon"]})
    assert await classify_service_intent("дни багги", openrouter=fake) is None


# --- parse_schedule (pure) --------------------------------------------------


def test_parse_schedule_hours_and_day_range() -> None:
    wh, days, err = parse_schedule(hours="08:00-21:00", days="mon-sun")
    assert err is None
    assert days == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    assert wh == {d: ["08:00", "21:00"] for d in days}


def test_parse_schedule_hours_without_days_defaults_to_all_week() -> None:
    wh, days, err = parse_schedule(hours="09:00-18:00", days=None)
    assert err is None
    assert days == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    assert wh["mon"] == ["09:00", "18:00"]


def test_parse_schedule_comma_separated_days() -> None:
    wh, days, err = parse_schedule(hours="10:00-19:00", days="mon,wed,fri")
    assert err is None
    assert days == ["mon", "wed", "fri"]
    assert set(wh) == {"mon", "wed", "fri"}


def test_parse_schedule_invalid_hours_shape() -> None:
    wh, days, err = parse_schedule(hours="с 8 до 9", days="mon-sun")
    assert (wh, days) == (None, None)
    assert err == "invalid_hours"


def test_parse_schedule_invalid_time_values() -> None:
    wh, days, err = parse_schedule(hours="25:00-26:00", days="mon-sun")
    assert err == "invalid_hours"


def test_parse_schedule_start_not_before_end() -> None:
    wh, days, err = parse_schedule(hours="21:00-08:00", days="mon-sun")
    assert err == "invalid_hours"


def test_parse_schedule_invalid_days() -> None:
    wh, days, err = parse_schedule(hours="08:00-21:00", days="funday")
    assert (wh, days) == (None, None)
    assert err == "invalid_days"


def test_parse_schedule_no_hours_returns_empty() -> None:
    wh, days, err = parse_schedule(hours=None, days="mon-fri")
    assert err is None
    assert wh is None


def test_parse_schedule_skips_empty_day_tokens() -> None:
    wh, days, err = parse_schedule(hours="08:00-21:00", days="mon,,sun")
    assert err is None
    assert days == ["mon", "sun"]


def test_parse_schedule_inverted_day_range_is_invalid() -> None:
    wh, days, err = parse_schedule(hours="08:00-21:00", days="sun-mon")
    assert err == "invalid_days"


def test_parse_schedule_range_with_unknown_endpoint_is_invalid() -> None:
    wh, days, err = parse_schedule(hours=None, days="mon-xyz")
    assert err == "invalid_days"


# --- dispatch / handler -----------------------------------------------------


def _msg(text: str, *, username: str = "@op") -> NormalizedTelegramMessage:
    return NormalizedTelegramMessage(
        update_id=1, source_message_id=2, chat_id=42, user_id=99,
        username=username, text=text,
    )


class _Sent:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def __call__(self, chat_id: int, text: str) -> None:
        self.calls.append((chat_id, text))


class FakeApi:
    def __init__(self) -> None:
        self.find_operator_by_username = AsyncMock(
            return_value={
                "username": "@op", "chat_id": 42, "project_id": 7, "is_active": True
            }
        )
        self.list_project_services = AsyncMock(return_value={"services": []})
        self.upsert_project_service = AsyncMock(return_value={"id": 99})


async def _run(api: FakeApi, sent: _Sent, payload: dict, *, text: str):
    return await handle_operator_service_nl_message(
        normalized=_msg(text),
        api_client=api,
        send_dm=sent,
        openrouter=_FakeOpenRouter(payload),
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )


@pytest.mark.asyncio
async def test_nl_schedule_updates_existing_preserving_description() -> None:
    api = FakeApi()
    api.list_project_services = AsyncMock(
        return_value={
            "services": [
                {
                    "id": 5, "project_id": 7, "name": "Аренда багги",
                    "description": "Прокат багги", "price_text": "5000₽",
                    "tags": ["outdoor"], "duration_minutes": 120,
                    "working_hours": {"mon": ["10:00", "12:00"]},
                    "service_days": ["mon"], "date_exceptions": ["2026-01-01"],
                }
            ]
        }
    )
    sent = _Sent()
    result = await _run(
        api, sent,
        {
            "action": "schedule", "name": "аренда багги",
            "hours": "08:00-21:00", "days": "mon-sun", "duration_minutes": None,
        },
        text="поставь часы работы аренда багги с 8 до 21 каждый день",
    )
    assert result is not None and result["status"] == "ok"
    assert result["route"] == "service_schedule"
    api.upsert_project_service.assert_awaited_once_with(
        project_id=7,
        actor="@op",
        actor_role="operator",
        internal_token="bot-tok",
        payload={
            "name": "Аренда багги",
            "description": "Прокат багги",
            "price_text": "5000₽",
            "tags": ["outdoor"],
            "duration_minutes": 120,
            "working_hours": {
                d: ["08:00", "21:00"]
                for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
            },
            "service_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            "date_exceptions": ["2026-01-01"],
        },
    )
    assert sent.calls == [
        (
            42,
            "Расписание обновлено: Аренда багги — 08:00–21:00, "
            "дни: mon,tue,wed,thu,fri,sat,sun.",
        )
    ]


@pytest.mark.asyncio
async def test_nl_schedule_creates_bookable_service_when_absent() -> None:
    api = FakeApi()  # list returns no services
    sent = _Sent()
    result = await _run(
        api, sent,
        {
            "action": "schedule", "name": "Аренда багги",
            "hours": "09:00-18:00", "days": "mon-fri", "duration_minutes": None,
        },
        text="сделай аренду багги бронируемой по будням с 9 до 18",
    )
    assert result is not None and result["status"] == "ok"
    kwargs = api.upsert_project_service.await_args.kwargs
    assert kwargs["payload"]["name"] == "Аренда багги"
    assert kwargs["payload"]["description"] is None
    assert kwargs["payload"]["duration_minutes"] == 60  # default min 1h
    assert kwargs["payload"]["service_days"] == ["mon", "tue", "wed", "thu", "fri"]
    assert sent.calls == [
        (
            42,
            "Услуга создана и сделана бронируемой: Аренда багги — "
            "09:00–18:00, дни: mon,tue,wed,thu,fri.",
        )
    ]


@pytest.mark.asyncio
async def test_nl_schedule_uses_provided_duration() -> None:
    api = FakeApi()
    sent = _Sent()
    await _run(
        api, sent,
        {
            "action": "schedule", "name": "йога", "hours": "08:00-20:00",
            "days": "mon-sun", "duration_minutes": 90,
        },
        text="часы работы йога с 8 до 20, занятие 90 минут",
    )
    assert api.upsert_project_service.await_args.kwargs["payload"][
        "duration_minutes"
    ] == 90


@pytest.mark.asyncio
async def test_nl_schedule_without_hours_sends_usage() -> None:
    api = FakeApi()
    sent = _Sent()
    result = await _run(
        api, sent,
        {"action": "schedule", "name": "багги", "hours": None, "days": "mon-sun"},
        text="настрой расписание багги",
    )
    assert result is not None and result["status"] == "error"
    assert result["decision"] == "missing_hours"
    api.upsert_project_service.assert_not_awaited()
    assert len(sent.calls) == 1 and "час" in sent.calls[0][1].lower()


@pytest.mark.asyncio
async def test_nl_schedule_invalid_hours_sends_error() -> None:
    api = FakeApi()
    sent = _Sent()
    result = await _run(
        api, sent,
        {"action": "schedule", "name": "багги", "hours": "восемь-девять",
         "days": "mon-sun"},
        text="часы работы багги восемь девять",
    )
    assert result is not None and result["status"] == "error"
    assert result["decision"] == "invalid_hours"
    api.upsert_project_service.assert_not_awaited()
    assert len(sent.calls) == 1


@pytest.mark.asyncio
async def test_nl_schedule_list_api_error_dms_unavailable() -> None:
    api = FakeApi()
    api.list_project_services = AsyncMock(side_effect=httpx.RequestError("boom"))
    sent = _Sent()
    result = await _run(
        api, sent,
        {"action": "schedule", "name": "багги", "hours": "08:00-21:00",
         "days": "mon-sun"},
        text="часы работы багги с 8 до 21",
    )
    assert result is not None and result["status"] == "error"
    api.upsert_project_service.assert_not_awaited()
    assert sent.calls == [(42, "Сервис временно недоступен, попробуйте позже.")]


@pytest.mark.asyncio
async def test_nl_schedule_upsert_api_error_dms_unavailable() -> None:
    api = FakeApi()
    api.upsert_project_service = AsyncMock(side_effect=httpx.RequestError("boom"))
    sent = _Sent()
    result = await _run(
        api, sent,
        {"action": "schedule", "name": "багги", "hours": "08:00-21:00",
         "days": "mon-sun"},
        text="часы работы багги с 8 до 21",
    )
    assert result is not None and result["status"] == "error"
    assert sent.calls == [(42, "Сервис временно недоступен, попробуйте позже.")]
