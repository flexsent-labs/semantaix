"""Bot-side admin NL dialog dispatcher tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from services.bot_gateway.app.admin_nl_dialog import handle_admin_nl_dialog
from services.bot_gateway.app.telegram_update import NormalizedTelegramMessage


def _msg(text: str, *, username: str = "@admin") -> NormalizedTelegramMessage:
    return NormalizedTelegramMessage(
        update_id=1,
        source_message_id=2,
        chat_id=10,
        user_id=99,
        username=username,
        text=text,
    )


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://api")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


class FakeApi:
    def __init__(self) -> None:
        self.admin_nl_ops_propose = AsyncMock(
            return_value={
                "id": 1,
                "status": "pending_confirmation",
                "confirm_token": "tok",
                "preview": "Создать проект…",
                "op_type": "project_create",
            }
        )
        self.admin_nl_ops_confirm = AsyncMock(
            return_value={"status": "confirmed", "op_type": "project_create"}
        )
        self.admin_nl_ops_cancel = AsyncMock(return_value={"status": "cancelled"})
        self.admin_nl_ops_latest_pending = AsyncMock(
            return_value={
                "found": True,
                "id": 1,
                "confirm_token": "tok",
                "preview": "p",
                "op_type": "project_create",
            }
        )


@pytest.fixture
def fake_api():
    return FakeApi()


@pytest.fixture
def send_dm():
    return AsyncMock()


@pytest.mark.asyncio
async def test_non_admin_ignored(fake_api, send_dm):
    result = await handle_admin_nl_dialog(
        normalized=_msg("создай проект x X", username="@user"),
        api_client=fake_api,
        send_dm=send_dm,
        admin_username="@admin",
    )
    assert result is None


@pytest.mark.asyncio
async def test_missing_username_ignored(fake_api, send_dm):
    result = await handle_admin_nl_dialog(
        normalized=_msg("создай проект x X", username=""),
        api_client=fake_api,
        send_dm=send_dm,
        admin_username="@admin",
    )
    assert result is None


@pytest.mark.asyncio
async def test_unmatched_returns_none(fake_api, send_dm):
    result = await handle_admin_nl_dialog(
        normalized=_msg("привет"),
        api_client=fake_api,
        send_dm=send_dm,
        admin_username="@admin",
    )
    assert result is None
    fake_api.admin_nl_ops_propose.assert_not_awaited()


@pytest.mark.asyncio
async def test_intent_propose_sends_preview(fake_api, send_dm):
    result = await handle_admin_nl_dialog(
        normalized=_msg("создай проект billing Биллинг"),
        api_client=fake_api,
        send_dm=send_dm,
        admin_username="@admin",
    )
    assert result == {
        "status": "ok",
        "route": "admin_nl_propose",
        "session_id": "1",
    }
    fake_api.admin_nl_ops_propose.assert_awaited_once()
    body = send_dm.await_args.args[1]
    assert "Создать проект" in body
    assert "/confirm tok" in body


@pytest.mark.asyncio
async def test_intent_clarify_sends_hint(fake_api, send_dm):
    fake_api.admin_nl_ops_propose.return_value = {
        "id": 2,
        "status": "clarify",
        "confirm_token": None,
        "preview": "Не понял. Попробуйте: создай проект <slug> <name>.",
    }
    result = await handle_admin_nl_dialog(
        normalized=_msg("создай проект"),
        api_client=fake_api,
        send_dm=send_dm,
        admin_username="@admin",
    )
    assert result["decision"] == "clarify"
    assert "Попробуйте" in send_dm.await_args.args[1]


@pytest.mark.asyncio
async def test_intent_propose_http_error(fake_api, send_dm):
    fake_api.admin_nl_ops_propose.side_effect = _http_error(500)
    result = await handle_admin_nl_dialog(
        normalized=_msg("создай проект billing B"),
        api_client=fake_api,
        send_dm=send_dm,
        admin_username="@admin",
    )
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_confirm_word_routes_to_confirm(fake_api, send_dm):
    result = await handle_admin_nl_dialog(
        normalized=_msg("да"),
        api_client=fake_api,
        send_dm=send_dm,
        admin_username="@admin",
    )
    assert result["route"] == "admin_nl_confirm"
    fake_api.admin_nl_ops_confirm.assert_awaited_once_with(
        session_id=1, confirm_token="tok"
    )
    assert "Операция применена" in send_dm.await_args.args[1]


@pytest.mark.asyncio
async def test_confirm_slash_with_token(fake_api, send_dm):
    result = await handle_admin_nl_dialog(
        normalized=_msg("/confirm tok"),
        api_client=fake_api,
        send_dm=send_dm,
        admin_username="@admin",
    )
    assert result["route"] == "admin_nl_confirm"


@pytest.mark.asyncio
async def test_confirm_slash_wrong_token_rejected(fake_api, send_dm):
    result = await handle_admin_nl_dialog(
        normalized=_msg("/confirm garbage"),
        api_client=fake_api,
        send_dm=send_dm,
        admin_username="@admin",
    )
    assert result["decision"] == "wrong_token"
    fake_api.admin_nl_ops_confirm.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_when_no_pending(fake_api, send_dm):
    fake_api.admin_nl_ops_latest_pending.return_value = {"found": False}
    result = await handle_admin_nl_dialog(
        normalized=_msg("да"),
        api_client=fake_api,
        send_dm=send_dm,
        admin_username="@admin",
    )
    assert result["decision"] == "no_pending"


@pytest.mark.asyncio
async def test_confirm_http_error_during_lookup(fake_api, send_dm):
    fake_api.admin_nl_ops_latest_pending.side_effect = _http_error(500)
    result = await handle_admin_nl_dialog(
        normalized=_msg("да"),
        api_client=fake_api,
        send_dm=send_dm,
        admin_username="@admin",
    )
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_confirm_http_error_during_confirm(fake_api, send_dm):
    fake_api.admin_nl_ops_confirm.side_effect = _http_error(500)
    result = await handle_admin_nl_dialog(
        normalized=_msg("да"),
        api_client=fake_api,
        send_dm=send_dm,
        admin_username="@admin",
    )
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_cancel_word(fake_api, send_dm):
    result = await handle_admin_nl_dialog(
        normalized=_msg("нет"),
        api_client=fake_api,
        send_dm=send_dm,
        admin_username="@admin",
    )
    assert result == {"status": "ok", "route": "admin_nl_cancel"}
    fake_api.admin_nl_ops_cancel.assert_awaited_once_with(session_id=1)


@pytest.mark.asyncio
async def test_cancel_slash(fake_api, send_dm):
    result = await handle_admin_nl_dialog(
        normalized=_msg("/cancel"),
        api_client=fake_api,
        send_dm=send_dm,
        admin_username="@admin",
    )
    assert result["route"] == "admin_nl_cancel"


@pytest.mark.asyncio
async def test_cancel_no_pending(fake_api, send_dm):
    fake_api.admin_nl_ops_latest_pending.return_value = {"found": False}
    result = await handle_admin_nl_dialog(
        normalized=_msg("/cancel"),
        api_client=fake_api,
        send_dm=send_dm,
        admin_username="@admin",
    )
    assert result["decision"] == "no_pending"


@pytest.mark.asyncio
async def test_cancel_http_error_during_lookup(fake_api, send_dm):
    fake_api.admin_nl_ops_latest_pending.side_effect = _http_error(500)
    result = await handle_admin_nl_dialog(
        normalized=_msg("/cancel"),
        api_client=fake_api,
        send_dm=send_dm,
        admin_username="@admin",
    )
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_cancel_http_error_during_cancel(fake_api, send_dm):
    fake_api.admin_nl_ops_cancel.side_effect = _http_error(500)
    result = await handle_admin_nl_dialog(
        normalized=_msg("/cancel"),
        api_client=fake_api,
        send_dm=send_dm,
        admin_username="@admin",
    )
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_intent_keywords_cover_phrasings(fake_api, send_dm):
    phrasings = [
        "Создай проект x",
        "Создайте проект y",
        "Переименуй проект x в Y",
        "Добавь оператора @op в x",
        "Удали оператора @op",
        "Привяжи файл #ABC к x",
    ]
    for phrase in phrasings:
        send_dm.reset_mock()
        await handle_admin_nl_dialog(
            normalized=_msg(phrase),
            api_client=fake_api,
            send_dm=send_dm,
            admin_username="@admin",
        )
        send_dm.assert_awaited_once()


def test_admin_nl_result_returned_via_webhook(tmp_path, monkeypatch):
    """Exercises the ``admin_nl_result is not None`` branch in the webhook
    dispatcher (lines 2637-2639 of bot_gateway/app/main.py)."""
    from fastapi.testclient import TestClient

    from services.bot_gateway.app import main as bot_main
    from services.bot_gateway.app.main import app as bot_app
    from services.bot_gateway.app.webhook_dedup import WebhookUpdateClaimRepository

    monkeypatch.setattr(
        bot_main,
        "webhook_update_claim_repository",
        WebhookUpdateClaimRepository(str(tmp_path / "dedup.sqlite3")),
    )
    monkeypatch.setattr(
        bot_main.api_client,
        "find_operator_by_username",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        bot_main,
        "handle_admin_nl_dialog",
        AsyncMock(return_value={"status": "nl_proposed"}),
    )

    response = TestClient(bot_app).post(
        "/telegram/webhook",
        json={
            "update_id": 88802,
            "message": {
                "message_id": 2,
                "from": {"id": 1, "username": "ajdevy"},
                "chat": {"id": 1, "type": "private"},
                "text": "создай проект Тест",
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "nl_proposed"
