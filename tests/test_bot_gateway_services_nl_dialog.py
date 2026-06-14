"""Bot-side services NL dialog dispatcher tests (Epic 13, story 13.05)."""

from __future__ import annotations

import json as _json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from services.bot_gateway.app import services_nl_dialog as dialog
from services.bot_gateway.app.api_client import ApiError
from services.bot_gateway.app.services_nl_dialog import (
    _reset_token_cache_for_tests,
    escape_and_cap,
    handle_services_nl_message,
)
from services.bot_gateway.app.telegram_update import NormalizedTelegramMessage


def _msg(text: str, *, username: str = "@op") -> NormalizedTelegramMessage:
    return NormalizedTelegramMessage(
        update_id=1,
        source_message_id=2,
        chat_id=42,
        user_id=99,
        username=username,
        text=text,
    )


def _api_error(status: int, detail: str) -> ApiError:
    body = _json.dumps({"detail": detail}).encode()
    request = httpx.Request("POST", "http://api")
    response = httpx.Response(status, content=body, request=request)
    return ApiError(
        "err",
        request=request,
        response=response,
        detail=detail,
    )


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://api")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


class FakeApi:
    """Minimal ApiClient stand-in. Each NL method is an AsyncMock the test
    rewires to return the canned response shape it needs."""

    def __init__(self) -> None:
        # Operator registry lookup — default returns the @op operator on
        # project 1; tests can override per-case.
        self.find_operator_by_username = AsyncMock(
            return_value={
                "username": "@op",
                "chat_id": 42,
                "project_id": 1,
                "is_active": True,
            }
        )
        self.services_nl_propose = AsyncMock(
            return_value={
                "session_id": 7,
                "status": "pending_confirmation",
                "preview": "Создать услугу «маникюр».",
                "confirm_token": "tok-xyz",
                "expires_at": "2026-05-24T12:00:00+00:00",
                "op_type": "service_add",
            }
        )
        self.services_nl_confirm = AsyncMock(
            return_value={
                "status": "confirmed",
                "applied_op_type": "service_add",
                "applied_service_id": 11,
            }
        )
        self.services_nl_cancel = AsyncMock(
            return_value={"status": "cancelled"}
        )
        self.services_nl_latest_pending = AsyncMock(
            return_value={"session_id": 7, "status": "pending_confirmation"}
        )


@pytest.fixture(autouse=True)
def _reset_cache():
    _reset_token_cache_for_tests()
    yield
    _reset_token_cache_for_tests()


@pytest.fixture
def fake_api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def send_dm():
    return AsyncMock()


def _send_dm_kwargs(send_dm: AsyncMock) -> list[dict[str, Any]]:
    """All capture parse_mode kwargs from every DM call (each must be None)."""
    return [c.kwargs for c in send_dm.await_args_list]


def _send_dm_texts(send_dm: AsyncMock) -> list[str]:
    return [c.args[1] for c in send_dm.await_args_list]


@pytest.mark.asyncio
async def test_non_trigger_message_returns_none(fake_api, send_dm):
    result = await handle_services_nl_message(
        normalized=_msg("привет"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result is None
    fake_api.services_nl_propose.assert_not_awaited()
    send_dm.assert_not_awaited()


@pytest.mark.asyncio
async def test_mid_message_trigger_does_not_match(fake_api, send_dm):
    # Anchored at start; "пожалуйста добавь услугу X" must NOT trigger.
    result = await handle_services_nl_message(
        normalized=_msg("пожалуйста добавь услугу маникюр"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result is None


@pytest.mark.asyncio
async def test_quoted_reply_does_not_trigger(fake_api, send_dm):
    result = await handle_services_nl_message(
        normalized=_msg("> добавь услугу маникюр"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result is None


@pytest.mark.asyncio
async def test_trigger_from_unregistered_returns_unauthorized_no_dm(
    fake_api, send_dm
):
    fake_api.find_operator_by_username = AsyncMock(return_value=None)
    result = await handle_services_nl_message(
        normalized=_msg("добавь услугу маникюр", username="@stranger"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result == {
        "status": "ignored",
        "reason": "unauthorized_services",
        "routed": "true",
    }
    send_dm.assert_not_awaited()
    fake_api.services_nl_propose.assert_not_awaited()


@pytest.mark.asyncio
async def test_propose_happy_path_sends_plain_preview(fake_api, send_dm):
    result = await handle_services_nl_message(
        normalized=_msg("добавь услугу маникюр на 60 минут"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result["status"] == "ok"
    assert result["route"] == "services_nl_propose"
    assert result["session_id"] == "7"
    fake_api.services_nl_propose.assert_awaited_once()
    # Exactly one DM with the preview + confirm/cancel instructions.
    assert send_dm.await_count == 1
    text = send_dm.await_args.args[1]
    assert "Создать услугу" in text
    assert "/confirm tok-xyz" in text
    assert "/cancel" in text
    # parse_mode is never passed (or is None).
    assert "parse_mode" not in send_dm.await_args.kwargs or (
        send_dm.await_args.kwargs.get("parse_mode") is None
    )


@pytest.mark.asyncio
async def test_propose_clarify_dms_only_preview(fake_api, send_dm):
    fake_api.services_nl_propose = AsyncMock(
        return_value={
            "session_id": 8,
            "status": "clarify",
            "preview": "не понял, добавьте по одной услуге за раз.",
            "op_type": "OP_UNKNOWN",
        }
    )
    result = await handle_services_nl_message(
        normalized=_msg("добавь услугу маникюр и педикюр"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result["decision"] == "clarify"
    assert send_dm.await_count == 1
    text = send_dm.await_args.args[1]
    assert "не понял" in text
    # No confirm/cancel suffix in clarify mode.
    assert "/confirm" not in text
    assert "/cancel" not in text


@pytest.mark.asyncio
async def test_propose_prior_cancelled_session_emits_two_dms_in_order(
    fake_api, send_dm
):
    fake_api.services_nl_propose = AsyncMock(
        return_value={
            "session_id": 9,
            "status": "pending_confirmation",
            "preview": "Создать услугу «педикюр».",
            "confirm_token": "tok-abc",
            "prior_cancelled_session_id": 7,
            "op_type": "service_add",
        }
    )
    await handle_services_nl_message(
        normalized=_msg("добавь услугу педикюр"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    texts = _send_dm_texts(send_dm)
    assert len(texts) == 2
    assert "предыдущий запрос отменён" in texts[0]
    assert "Создать услугу «педикюр»" in texts[1]
    assert "/confirm tok-abc" in texts[1]


@pytest.mark.asyncio
async def test_propose_api_error_dms_russian_error(fake_api, send_dm):
    fake_api.services_nl_propose = AsyncMock(
        side_effect=_api_error(500, "boom")
    )
    result = await handle_services_nl_message(
        normalized=_msg("добавь услугу маникюр"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result["status"] == "error"
    assert send_dm.await_count == 1
    assert "Ошибка" in send_dm.await_args.args[1]


@pytest.mark.asyncio
async def test_propose_http_status_error_path(fake_api, send_dm):
    fake_api.services_nl_propose = AsyncMock(
        side_effect=_http_error(503)
    )
    result = await handle_services_nl_message(
        normalized=_msg("добавь услугу маникюр"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result["status"] == "error"
    assert "503" in send_dm.await_args.args[1]


@pytest.mark.asyncio
async def test_propose_missing_token_dms_unavailable(fake_api, send_dm):
    fake_api.services_nl_propose = AsyncMock(
        return_value={
            "session_id": 7,
            "status": "pending_confirmation",
            "preview": "X",
            # confirm_token missing.
        }
    )
    result = await handle_services_nl_message(
        normalized=_msg("добавь услугу маникюр"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result["status"] == "error"
    assert "недоступен" in send_dm.await_args.args[1]


# --- Confirm flow ----------------------------------------------------------


async def _seed_cached_pending(fake_api, send_dm) -> None:
    await handle_services_nl_message(
        normalized=_msg("добавь услугу маникюр"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    send_dm.reset_mock()


@pytest.mark.asyncio
async def test_da_word_routes_to_confirm(fake_api, send_dm):
    await _seed_cached_pending(fake_api, send_dm)
    result = await handle_services_nl_message(
        normalized=_msg("да"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result["route"] == "services_nl_confirm"
    fake_api.services_nl_confirm.assert_awaited_once()
    call = fake_api.services_nl_confirm.await_args
    assert call.kwargs["project_id"] == 1
    assert call.kwargs["session_id"] == 7
    assert call.kwargs["confirm_token"] == "tok-xyz"
    assert call.kwargs["presenter_operator"] == "@op"
    assert "Операция применена: service_add" in send_dm.await_args.args[1]


@pytest.mark.asyncio
async def test_da_with_no_cached_falls_through(fake_api, send_dm):
    result = await handle_services_nl_message(
        normalized=_msg("да"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    # No cached → silent None so other handlers may take over.
    assert result is None
    fake_api.services_nl_confirm.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_slash_with_explicit_token(fake_api, send_dm):
    valid_token = "A" * 22  # 22 url-safe chars
    fake_api.services_nl_latest_pending = AsyncMock(
        return_value={"session_id": 77, "status": "pending_confirmation"}
    )
    result = await handle_services_nl_message(
        normalized=_msg(f"/confirm {valid_token}"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result["route"] == "services_nl_confirm"
    fake_api.services_nl_confirm.assert_awaited_once()
    call = fake_api.services_nl_confirm.await_args
    assert call.kwargs["session_id"] == 77
    assert call.kwargs["confirm_token"] == valid_token


@pytest.mark.asyncio
async def test_confirm_slash_invalid_token_regex_falls_through(fake_api, send_dm):
    # /confirm with a too-short / non-url-safe token does not match the
    # tight regex, so the dispatcher returns None and does not call api.
    result = await handle_services_nl_message(
        normalized=_msg("/confirm short"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result is None
    fake_api.services_nl_confirm.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_slash_no_token_uses_latest(fake_api, send_dm):
    # /confirm with no token argument is matched by the regex (optional
    # group). When there is also no cached pending and no explicit token,
    # the dispatcher must DM "no pending" rather than crashing.
    fake_api.services_nl_latest_pending = AsyncMock(return_value=None)
    result = await handle_services_nl_message(
        normalized=_msg("/confirm"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    # Without an explicit token AND no cached entry, falls to "no_pending".
    assert result["decision"] == "no_pending"


@pytest.mark.asyncio
async def test_confirm_slash_explicit_token_latest_404(fake_api, send_dm):
    valid_token = "B" * 22
    fake_api.services_nl_latest_pending = AsyncMock(return_value=None)
    result = await handle_services_nl_message(
        normalized=_msg(f"/confirm {valid_token}"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result["decision"] == "no_pending"
    assert "Нет ожидающих" in send_dm.await_args.args[1]


@pytest.mark.asyncio
async def test_confirm_slash_explicit_token_latest_http_error(fake_api, send_dm):
    valid_token = "C" * 22
    fake_api.services_nl_latest_pending = AsyncMock(
        side_effect=_api_error(500, "boom")
    )
    result = await handle_services_nl_message(
        normalized=_msg(f"/confirm {valid_token}"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result["status"] == "error"
    assert "Ошибка поиска" in send_dm.await_args.args[1]


@pytest.mark.asyncio
async def test_confirm_401_invalid_token(fake_api, send_dm):
    await _seed_cached_pending(fake_api, send_dm)
    fake_api.services_nl_confirm = AsyncMock(
        side_effect=_api_error(401, "invalid_confirm_token")
    )
    result = await handle_services_nl_message(
        normalized=_msg("да"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result["status"] == "error"
    assert send_dm.await_args.args[1] == "Неверный токен."


@pytest.mark.asyncio
async def test_confirm_403_not_session_owner(fake_api, send_dm):
    await _seed_cached_pending(fake_api, send_dm)
    fake_api.services_nl_confirm = AsyncMock(
        side_effect=_api_error(403, "not_session_owner")
    )
    result = await handle_services_nl_message(
        normalized=_msg("да"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result["status"] == "error"
    assert "не принадлежит" in send_dm.await_args.args[1]


@pytest.mark.asyncio
async def test_confirm_403_admin_cannot_remove_service(fake_api, send_dm):
    await _seed_cached_pending(fake_api, send_dm)
    fake_api.services_nl_confirm = AsyncMock(
        side_effect=_api_error(403, "admin_cannot_remove_service")
    )
    result = await handle_services_nl_message(
        normalized=_msg("да"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result["status"] == "error"
    assert "только оператору" in send_dm.await_args.args[1]


@pytest.mark.asyncio
async def test_confirm_410_session_expired(fake_api, send_dm):
    await _seed_cached_pending(fake_api, send_dm)
    fake_api.services_nl_confirm = AsyncMock(
        side_effect=_api_error(410, "session_expired")
    )
    result = await handle_services_nl_message(
        normalized=_msg("да"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result["status"] == "error"
    assert "истекла" in send_dm.await_args.args[1]


@pytest.mark.asyncio
async def test_confirm_410_session_not_pending(fake_api, send_dm):
    await _seed_cached_pending(fake_api, send_dm)
    fake_api.services_nl_confirm = AsyncMock(
        side_effect=_api_error(410, "session_not_pending:cancelled")
    )
    result = await handle_services_nl_message(
        normalized=_msg("да"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result["status"] == "error"
    assert "уже применена или отменена" in send_dm.await_args.args[1]


@pytest.mark.asyncio
async def test_confirm_http_status_error_path(fake_api, send_dm):
    await _seed_cached_pending(fake_api, send_dm)
    fake_api.services_nl_confirm = AsyncMock(side_effect=_http_error(503))
    result = await handle_services_nl_message(
        normalized=_msg("да"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result["status"] == "error"
    assert "503" in send_dm.await_args.args[1]


@pytest.mark.asyncio
async def test_confirm_unmapped_detail_falls_to_generic(fake_api, send_dm):
    await _seed_cached_pending(fake_api, send_dm)
    fake_api.services_nl_confirm = AsyncMock(
        side_effect=_api_error(400, "weird_error")
    )
    result = await handle_services_nl_message(
        normalized=_msg("да"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result["status"] == "error"
    assert "Не удалось подтвердить" in send_dm.await_args.args[1]


# --- Cancel flow -----------------------------------------------------------


@pytest.mark.asyncio
async def test_net_word_cancels(fake_api, send_dm):
    await _seed_cached_pending(fake_api, send_dm)
    result = await handle_services_nl_message(
        normalized=_msg("нет"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result == {"status": "ok", "route": "services_nl_cancel"}
    fake_api.services_nl_cancel.assert_awaited_once()
    assert send_dm.await_args.args[1] == "Запрос отменён."


@pytest.mark.asyncio
async def test_net_word_no_cached_falls_through(fake_api, send_dm):
    result = await handle_services_nl_message(
        normalized=_msg("нет"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result is None
    fake_api.services_nl_cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_slash_uses_latest_when_no_cache(fake_api, send_dm):
    fake_api.services_nl_latest_pending = AsyncMock(
        return_value={"session_id": 99, "status": "pending_confirmation"}
    )
    result = await handle_services_nl_message(
        normalized=_msg("/cancel"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result["status"] == "ok"
    call = fake_api.services_nl_cancel.await_args
    assert call.kwargs["session_id"] == 99


@pytest.mark.asyncio
async def test_cancel_slash_no_latest_dms_no_pending(fake_api, send_dm):
    fake_api.services_nl_latest_pending = AsyncMock(return_value=None)
    result = await handle_services_nl_message(
        normalized=_msg("/cancel"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result["decision"] == "no_pending"


@pytest.mark.asyncio
async def test_cancel_slash_latest_lookup_error(fake_api, send_dm):
    fake_api.services_nl_latest_pending = AsyncMock(
        side_effect=_api_error(500, "boom")
    )
    result = await handle_services_nl_message(
        normalized=_msg("/cancel"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_cancel_api_error_path(fake_api, send_dm):
    await _seed_cached_pending(fake_api, send_dm)
    fake_api.services_nl_cancel = AsyncMock(
        side_effect=_api_error(410, "session_not_pending:cancelled")
    )
    result = await handle_services_nl_message(
        normalized=_msg("/cancel"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result["status"] == "error"
    assert "уже применена или отменена" in send_dm.await_args.args[1]


@pytest.mark.asyncio
async def test_cancel_http_status_error_path(fake_api, send_dm):
    await _seed_cached_pending(fake_api, send_dm)
    fake_api.services_nl_cancel = AsyncMock(side_effect=_http_error(503))
    result = await handle_services_nl_message(
        normalized=_msg("/cancel"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result["status"] == "error"
    assert "503" in send_dm.await_args.args[1]


@pytest.mark.asyncio
async def test_cancel_unmapped_detail_falls_to_generic(fake_api, send_dm):
    await _seed_cached_pending(fake_api, send_dm)
    fake_api.services_nl_cancel = AsyncMock(
        side_effect=_api_error(400, "weird")
    )
    await handle_services_nl_message(
        normalized=_msg("/cancel"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert "Не удалось отменить" in send_dm.await_args.args[1]


@pytest.mark.asyncio
async def test_cancel_403_not_session_owner(fake_api, send_dm):
    await _seed_cached_pending(fake_api, send_dm)
    fake_api.services_nl_cancel = AsyncMock(
        side_effect=_api_error(403, "not_session_owner")
    )
    await handle_services_nl_message(
        normalized=_msg("/cancel"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert "не принадлежит" in send_dm.await_args.args[1]


# --- Plain-text assertion, length-cap helper -------------------------------


@pytest.mark.asyncio
async def test_every_dm_call_has_no_parse_mode_kwarg(fake_api, send_dm):
    await _seed_cached_pending(fake_api, send_dm)
    await handle_services_nl_message(
        normalized=_msg("да"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    for kwargs in _send_dm_kwargs(send_dm):
        # Either omitted entirely or set to None.
        assert kwargs.get("parse_mode") is None


def test_escape_and_cap_strips_control_chars_and_caps():
    raw = "hello\x00world" + ("x" * 250)
    out = escape_and_cap(raw, max_len=200)
    assert "\x00" not in out
    assert out.endswith("…")
    assert len(out) <= 201  # 200 chars + 1 ellipsis


def test_escape_and_cap_none_returns_empty():
    assert escape_and_cap(None) == ""


def test_escape_and_cap_keeps_short_value():
    assert escape_and_cap("маникюр") == "маникюр"


# --- Trigger keyword regex parametric checks -------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phrase",
    [
        "добавь услугу маникюр",
        "добавьте услугу маникюр",
        "новая услуга маникюр",  # ru nom. case — caught by the same regex
        "создай услугу маникюр",
        "удали услугу маникюр",
        "удалите услугу маникюр",
        "измени услугу маникюр",
        "измените услугу маникюр",
    ],
)
async def test_trigger_keywords_route_to_propose(fake_api, send_dm, phrase):
    result = await handle_services_nl_message(
        normalized=_msg(phrase),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    # Defense-in-depth: the bot trigger regex accepts the same inflection
    # set as the api parser (``услуг[ауы]\b``) so a phrase the api accepts
    # cannot be silently dropped at the bot edge.
    assert result is not None
    assert result["route"] == "services_nl_propose"


@pytest.mark.asyncio
async def test_yo_normalized_trigger_matches(fake_api, send_dm):
    # Operator typed "ё" anywhere → still matches.
    result = await handle_services_nl_message(
        normalized=_msg("добавь Услугу ёлка"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result is not None


@pytest.mark.asyncio
async def test_inactive_operator_unauthorized(fake_api, send_dm):
    fake_api.find_operator_by_username = AsyncMock(
        return_value={
            "username": "@op",
            "chat_id": 42,
            "project_id": 1,
            "is_active": False,
        }
    )
    result = await handle_services_nl_message(
        normalized=_msg("добавь услугу маникюр"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result["reason"] == "unauthorized_services"
    send_dm.assert_not_awaited()


@pytest.mark.asyncio
async def test_primary_fallback_without_project_is_unauthorized(fake_api, send_dm):
    # find_operator_by_username raises HTTP 5xx → resolver returns None
    # (fail-closed) → trigger present → unauthorized.
    fake_api.find_operator_by_username = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "err",
            request=httpx.Request("GET", "http://api"),
            response=httpx.Response(500),
        )
    )
    result = await handle_services_nl_message(
        normalized=_msg("добавь услугу маникюр", username="@primary"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result["reason"] == "unauthorized_services"
    send_dm.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_text_short_circuits(fake_api, send_dm):
    """An empty-text update never reaches the trigger regex / api."""
    result = await handle_services_nl_message(
        normalized=_msg(""),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result is None
    fake_api.find_operator_by_username.assert_not_called()


@pytest.mark.asyncio
async def test_confirm_word_from_non_operator_returns_none(fake_api, send_dm):
    """A bare ``да`` from someone the registry does not recognise must
    fall through (return None) so the rest of the pipeline can handle it
    — e.g. a customer typing «да» mid-conversation should NOT be silenced
    by the services-NL dispatcher."""
    fake_api.find_operator_by_username = AsyncMock(return_value=None)
    result = await handle_services_nl_message(
        normalized=_msg("да", username="@customer"),
        api_client=fake_api,
        send_dm=send_dm,
        internal_token="t",
    )
    assert result is None
    send_dm.assert_not_awaited()


@pytest.mark.asyncio
async def test_module_level_dialog_export_callable():
    # Smoke check the public entry point and a private helper that the
    # tests intentionally pin so a refactor cannot silently change names.
    assert callable(dialog.handle_services_nl_message)
    assert callable(dialog.escape_and_cap)
