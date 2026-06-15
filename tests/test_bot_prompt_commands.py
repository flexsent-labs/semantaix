"""Tests for the bot gateway's prompt slash commands + pending-edit dispatch."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from services.bot_gateway.app.api_client import ApiClient, ApiError
from services.bot_gateway.app.prompt_commands import (
    PROMPT_COMMAND_PREFIXES,
    _truncate_for_telegram,
    dispatch_pending_prompt_edit,
    handle_prompt_command,
)
from services.bot_gateway.app.telegram_update import NormalizedTelegramMessage


def _msg(
    text: str,
    *,
    username: str | None = "@alice",
    chat_id: int | None = 11,
) -> NormalizedTelegramMessage:
    return NormalizedTelegramMessage(
        update_id=1,
        source_message_id=1,
        chat_id=chat_id if chat_id is not None else 0,
        user_id=42,
        username=username,
        text=text,
    )


def _api_error(status: int, detail: str) -> ApiError:
    request = httpx.Request("GET", "http://test")
    response = httpx.Response(status, request=request, json={"detail": detail})
    return ApiError(
        f"HTTP {status}",
        request=request,
        response=response,
        detail=detail,
    )


def _stub_send_dm() -> tuple[Callable[[int, str], Awaitable[Any]], list[tuple[int, str]]]:
    calls: list[tuple[int, str]] = []

    async def send(chat_id: int, text: str) -> dict[str, str]:
        calls.append((chat_id, text))
        return {"ok": "1"}

    return send, calls


def _api_mock(spec=None) -> ApiClient:
    if spec is None:
        return MagicMock(spec=ApiClient)
    return spec


# ---------------------------------------------------------------------------
# Prefix gating + usage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_prompt_command_returns_none():
    api = _api_mock()
    send, _ = _stub_send_dm()
    result = await handle_prompt_command(
        normalized=_msg("hello"),
        api_client=api,
        send_dm=send,
        internal_token="t",
    )
    assert result is None


@pytest.mark.asyncio
async def test_missing_username_returns_none():
    api = _api_mock()
    send, _ = _stub_send_dm()
    assert (
        await handle_prompt_command(
            normalized=_msg("/prompts default", username=None),
            api_client=api,
            send_dm=send,
            internal_token="t",
        )
        is None
    )
    assert (
        await dispatch_pending_prompt_edit(
            normalized=_msg("text", username=None),
            api_client=api,
            send_dm=send,
            internal_token="t",
        )
        is None
    )


@pytest.mark.asyncio
async def test_malformed_prompt_set_shows_usage_hint():
    """A message that starts with a known prefix but fails every regex
    falls through to the usage hint."""
    api = _api_mock()
    send, calls = _stub_send_dm()
    result = await handle_prompt_command(
        normalized=_msg("/prompt_set"),
        api_client=api,
        send_dm=send,
        internal_token="t",
    )
    assert result == {"status": "ok", "route": "prompt_usage_hint"}
    assert "/prompt_set" in calls[0][1]


@pytest.mark.asyncio
async def test_unknown_prompt_subcommand_is_not_routed():
    """A command that doesn't share a known prefix returns None so the next
    dispatcher in the chain can handle it."""
    api = _api_mock()
    send, calls = _stub_send_dm()
    result = await handle_prompt_command(
        normalized=_msg("/prompt_unknown stuff"),
        api_client=api,
        send_dm=send,
        internal_token="t",
    )
    assert result is None
    assert calls == []


# ---------------------------------------------------------------------------
# /prompts (list)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompts_list_without_slug_prints_usage():
    api = _api_mock()
    send, calls = _stub_send_dm()
    result = await handle_prompt_command(
        normalized=_msg("/prompts"),
        api_client=api,
        send_dm=send,
        internal_token="t",
    )
    assert result == {"status": "ok", "route": "prompt_list_usage"}
    assert "/prompts" in calls[0][1]


@pytest.mark.asyncio
async def test_prompts_list_renders_items():
    api = MagicMock(spec=ApiClient)
    api.list_project_prompts = AsyncMock(
        return_value={
            "items": [
                {"prompt_name": "grounding_system", "version": 0, "is_default": True},
                {"prompt_name": "verifier_system", "version": 2, "is_default": False},
            ]
        }
    )
    send, calls = _stub_send_dm()
    result = await handle_prompt_command(
        normalized=_msg("/prompts default"),
        api_client=api,
        send_dm=send,
        internal_token="t",
    )
    assert result == {"status": "ok", "route": "prompt_list"}
    text = calls[0][1]
    assert "grounding_system" in text
    assert "verifier_system" in text
    assert "default" in text
    assert "override" in text


@pytest.mark.asyncio
async def test_prompts_list_reports_api_error():
    api = MagicMock(spec=ApiClient)
    api.list_project_prompts = AsyncMock(
        side_effect=_api_error(403, "not_in_project")
    )
    send, calls = _stub_send_dm()
    result = await handle_prompt_command(
        normalized=_msg("/prompts other"),
        api_client=api,
        send_dm=send,
        internal_token="t",
    )
    assert result["status"] == "error"
    assert "not_in_project" in calls[0][1]


# ---------------------------------------------------------------------------
# /prompt_show
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_show_returns_value():
    api = MagicMock(spec=ApiClient)
    api.get_project_prompt = AsyncMock(
        return_value={
            "value": "the verifier",
            "version": 1,
            "is_default": False,
            "history": [],
        }
    )
    send, calls = _stub_send_dm()
    result = await handle_prompt_command(
        normalized=_msg("/prompt_show default verifier_system"),
        api_client=api,
        send_dm=send,
        internal_token="t",
    )
    assert result == {"status": "ok", "route": "prompt_show"}
    assert "the verifier" in calls[0][1]


@pytest.mark.asyncio
async def test_prompt_show_api_error_message():
    api = MagicMock(spec=ApiClient)
    api.get_project_prompt = AsyncMock(
        side_effect=_api_error(404, "unknown_prompt_name")
    )
    send, calls = _stub_send_dm()
    result = await handle_prompt_command(
        normalized=_msg("/prompt_show default bogus"),
        api_client=api,
        send_dm=send,
        internal_token="t",
    )
    assert result["status"] == "error"
    assert "unknown_prompt_name" in calls[0][1]


def test_truncate_for_telegram_keeps_short_text():
    assert _truncate_for_telegram("short") == "short"


def test_truncate_for_telegram_clamps_long_text():
    big = "a" * 5000
    out = _truncate_for_telegram(big)
    assert "truncated" in out
    assert len(out) <= 3500


# ---------------------------------------------------------------------------
# /prompt_set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_set_arms_pending_and_acks():
    api = MagicMock(spec=ApiClient)
    api.arm_prompt_pending_edit = AsyncMock(return_value={"armed_for": "@alice"})
    send, calls = _stub_send_dm()
    result = await handle_prompt_command(
        normalized=_msg("/prompt_set default verifier_system"),
        api_client=api,
        send_dm=send,
        internal_token="t",
    )
    assert result == {"status": "ok", "route": "prompt_set_armed"}
    api.arm_prompt_pending_edit.assert_awaited_once_with(
        project_slug="default",
        prompt_name="verifier_system",
        requester_username="@alice",
        internal_token="t",
    )
    assert "verifier_system" in calls[0][1]


@pytest.mark.asyncio
async def test_prompt_set_api_error():
    api = MagicMock(spec=ApiClient)
    api.arm_prompt_pending_edit = AsyncMock(
        side_effect=_api_error(404, "project_not_found")
    )
    send, calls = _stub_send_dm()
    result = await handle_prompt_command(
        normalized=_msg("/prompt_set ghost verifier_system"),
        api_client=api,
        send_dm=send,
        internal_token="t",
    )
    assert result["status"] == "error"
    assert "project_not_found" in calls[0][1]


# ---------------------------------------------------------------------------
# /prompt_cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_cancel_when_pending_deleted():
    api = MagicMock(spec=ApiClient)
    api.cancel_pending_prompt_edit = AsyncMock(return_value={"deleted": True})
    send, calls = _stub_send_dm()
    result = await handle_prompt_command(
        normalized=_msg("/prompt_cancel"),
        api_client=api,
        send_dm=send,
        internal_token="t",
    )
    assert result == {"status": "ok", "route": "prompt_cancel"}
    assert "Отменено" in calls[0][1]


@pytest.mark.asyncio
async def test_prompt_cancel_when_nothing_pending():
    api = MagicMock(spec=ApiClient)
    api.cancel_pending_prompt_edit = AsyncMock(return_value={"deleted": False})
    send, calls = _stub_send_dm()
    await handle_prompt_command(
        normalized=_msg("/prompt_cancel"),
        api_client=api,
        send_dm=send,
        internal_token="t",
    )
    assert "Активного" in calls[0][1]


# ---------------------------------------------------------------------------
# /prompt_history + /prompt_restore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_history_lists_versions():
    api = MagicMock(spec=ApiClient)
    api.get_project_prompt = AsyncMock(
        return_value={
            "value": "x",
            "history": [
                {"version": 3, "edited_by": "@a", "created_at": "2026-05-20T10:00:00+00:00"},
                {"version": 2, "edited_by": "@b", "created_at": "2026-05-19T10:00:00+00:00"},
            ],
        }
    )
    send, calls = _stub_send_dm()
    result = await handle_prompt_command(
        normalized=_msg("/prompt_history default verifier_system"),
        api_client=api,
        send_dm=send,
        internal_token="t",
    )
    assert result == {"status": "ok", "route": "prompt_history"}
    assert "v3" in calls[0][1] and "v2" in calls[0][1]


@pytest.mark.asyncio
async def test_prompt_history_empty():
    api = MagicMock(spec=ApiClient)
    api.get_project_prompt = AsyncMock(
        return_value={"value": "x", "history": []}
    )
    send, calls = _stub_send_dm()
    result = await handle_prompt_command(
        normalized=_msg("/prompt_history default verifier_system"),
        api_client=api,
        send_dm=send,
        internal_token="t",
    )
    assert result == {"status": "ok", "route": "prompt_history_empty"}
    assert "пуста" in calls[0][1]


@pytest.mark.asyncio
async def test_prompt_history_api_error():
    api = MagicMock(spec=ApiClient)
    api.get_project_prompt = AsyncMock(
        side_effect=_api_error(404, "project_not_found")
    )
    send, calls = _stub_send_dm()
    result = await handle_prompt_command(
        normalized=_msg("/prompt_history ghost verifier_system"),
        api_client=api,
        send_dm=send,
        internal_token="t",
    )
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_prompt_restore_calls_api():
    api = MagicMock(spec=ApiClient)
    api.restore_project_prompt = AsyncMock(return_value={"version": 5})
    send, calls = _stub_send_dm()
    result = await handle_prompt_command(
        normalized=_msg("/prompt_restore default verifier_system 2"),
        api_client=api,
        send_dm=send,
        internal_token="t",
    )
    assert result == {"status": "ok", "route": "prompt_restore"}
    api.restore_project_prompt.assert_awaited_once_with(
        project_slug="default",
        prompt_name="verifier_system",
        version=2,
        requester_username="@alice",
        internal_token="t",
    )
    assert "v5" in calls[0][1]


@pytest.mark.asyncio
async def test_prompt_restore_api_error():
    api = MagicMock(spec=ApiClient)
    api.restore_project_prompt = AsyncMock(
        side_effect=_api_error(404, "version_not_found")
    )
    send, calls = _stub_send_dm()
    result = await handle_prompt_command(
        normalized=_msg("/prompt_restore default verifier_system 99"),
        api_client=api,
        send_dm=send,
        internal_token="t",
    )
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Pending-edit dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_skips_command_messages():
    api = MagicMock(spec=ApiClient)
    api.peek_pending_prompt_edit = AsyncMock()
    send, _ = _stub_send_dm()
    result = await dispatch_pending_prompt_edit(
        normalized=_msg("/prompt_cancel"),
        api_client=api,
        send_dm=send,
        internal_token="t",
    )
    assert result is None
    api.peek_pending_prompt_edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_skips_empty_text_or_anonymous():
    api = MagicMock(spec=ApiClient)
    api.peek_pending_prompt_edit = AsyncMock()
    send, _ = _stub_send_dm()
    assert (
        await dispatch_pending_prompt_edit(
            normalized=_msg(""),
            api_client=api,
            send_dm=send,
            internal_token="t",
        )
        is None
    )
    assert (
        await dispatch_pending_prompt_edit(
            normalized=_msg("hi", username=None),
            api_client=api,
            send_dm=send,
            internal_token="t",
        )
        is None
    )
    api.peek_pending_prompt_edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_returns_none_when_no_pending():
    api = MagicMock(spec=ApiClient)
    api.peek_pending_prompt_edit = AsyncMock(return_value=None)
    send, _ = _stub_send_dm()
    result = await dispatch_pending_prompt_edit(
        normalized=_msg("new prompt text"),
        api_client=api,
        send_dm=send,
        internal_token="t",
    )
    assert result is None


@pytest.mark.asyncio
async def test_dispatch_consumes_and_confirms_when_pending():
    api = MagicMock(spec=ApiClient)
    api.peek_pending_prompt_edit = AsyncMock(
        return_value={
            "project_slug": "default",
            "prompt_name": "verifier_system",
        }
    )
    api.consume_pending_prompt_edit = AsyncMock(
        return_value={
            "version": 3,
            "prompt_name": "verifier_system",
            "project_slug": "default",
        }
    )
    send, calls = _stub_send_dm()
    result = await dispatch_pending_prompt_edit(
        normalized=_msg("new verifier instructions"),
        api_client=api,
        send_dm=send,
        internal_token="t",
    )
    assert result == {"status": "ok", "route": "prompt_pending_consumed"}
    api.consume_pending_prompt_edit.assert_awaited_once_with(
        value="new verifier instructions",
        requester_username="@alice",
        internal_token="t",
    )
    assert "v3" in calls[0][1]


@pytest.mark.asyncio
async def test_dispatch_truncates_preview_for_long_inputs():
    api = MagicMock(spec=ApiClient)
    api.peek_pending_prompt_edit = AsyncMock(
        return_value={"project_slug": "default", "prompt_name": "verifier_system"}
    )
    api.consume_pending_prompt_edit = AsyncMock(
        return_value={
            "version": 1,
            "prompt_name": "verifier_system",
            "project_slug": "default",
        }
    )
    send, calls = _stub_send_dm()
    big = "x" * 500
    await dispatch_pending_prompt_edit(
        normalized=_msg(big),
        api_client=api,
        send_dm=send,
        internal_token="t",
    )
    assert "…" in calls[0][1]


@pytest.mark.asyncio
async def test_dispatch_reports_api_error_on_consume():
    api = MagicMock(spec=ApiClient)
    api.peek_pending_prompt_edit = AsyncMock(
        return_value={"project_slug": "default", "prompt_name": "grounding_system"}
    )
    api.consume_pending_prompt_edit = AsyncMock(
        side_effect=_api_error(
            422,
            "grounding_system: must contain {name} and {today_iso}",
        )
    )
    send, calls = _stub_send_dm()
    result = await dispatch_pending_prompt_edit(
        normalized=_msg("not valid grounding text"),
        api_client=api,
        send_dm=send,
        internal_token="t",
    )
    assert result["status"] == "error"
    assert "must contain" in calls[0][1]


def test_prompt_command_prefixes_match_module_constants():
    assert "/prompts" in PROMPT_COMMAND_PREFIXES
    assert "/prompt_set" in PROMPT_COMMAND_PREFIXES


def test_format_detail_empty_when_detail_none():
    """ApiError with detail=None produces an empty string suffix."""
    from services.bot_gateway.app.prompt_commands import _format_detail

    request = httpx.Request("GET", "http://test")
    response = httpx.Response(500, request=request)
    err = ApiError(
        "boom", request=request, response=response, detail=None
    )
    assert _format_detail(err) == ""


def test_prompt_command_result_returned_via_webhook(tmp_path, monkeypatch):
    """Exercises the ``prompt_command_result is not None`` branch in the webhook
    dispatcher (lines 2613-2615 of bot_gateway/app/main.py)."""
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
        "handle_prompt_command",
        AsyncMock(return_value={"status": "prompts_listed"}),
    )

    response = TestClient(bot_app).post(
        "/telegram/webhook",
        json={
            "update_id": 88801,
            "message": {
                "message_id": 1,
                "from": {"id": 1, "username": "ajdevy"},
                "chat": {"id": 1, "type": "private"},
                "text": "/prompts",
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "prompts_listed"
