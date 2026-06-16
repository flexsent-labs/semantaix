"""Tests for usage_command.handle_usage_command routing (Story 14.08)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.bot_gateway.app.operator_resolver import ResolvedOperator
from services.bot_gateway.app.usage_command import handle_usage_command

_RESOLVER = "services.bot_gateway.app.usage_command.resolve_operator_for_sender"


def _normalized(text: str, username: str = "@op", chat_id: int = 100):
    msg = MagicMock()
    msg.text = text
    msg.username = username
    msg.chat_id = chat_id
    return msg


def _api_client(
    *,
    operator: ResolvedOperator | None = None,
    fetch_result: dict | None = None,
    projects: list[dict] | None = None,
):
    client = MagicMock()
    client.find_operator_by_username = AsyncMock(
        return_value=(
            {
                "username": operator.username,
                "chat_id": operator.chat_id,
                "project_id": operator.project_id,
                "is_active": True,
            }
            if operator
            else None
        )
    )
    client.fetch_usage_today = AsyncMock(return_value=fetch_result)
    client.list_projects = AsyncMock(return_value={"items": projects or []})
    return client


_OP = ResolvedOperator(
    username="@op", chat_id=100, project_id=5, is_active=True, source="registry"
)
_USAGE_RESULT = {
    "summary_rows": [
        {
            "tracker_type": "messages",
            "model_name": "",
            "prompt_tokens_total": None,
            "completion_tokens_total": None,
            "cost_usd_total": None,
            "wasted_cost_usd": None,
            "call_count": None,
            "in_count": 5,
            "out_count": 4,
            "hitl_created_count": None,
            "hitl_assigned_count": None,
            "hitl_replied_count": None,
            "hitl_resolved_count": None,
        }
    ],
    "wasted_rows": None,
}
_PROJECTS = [{"id": 5, "slug": "salon", "name": "Салон"}]


@pytest.mark.asyncio
async def test_nonusage_message_returns_none():
    result = await handle_usage_command(
        normalized=_normalized("привет"),
        api_client=_api_client(operator=_OP),
        send_dm=AsyncMock(),
        admin_username="@admin",
        internal_token="tok",
        web_ui_base_url="http://ui:8001",
        default_timezone="UTC",
    )
    assert result is None


@pytest.mark.asyncio
async def test_unregistered_sender_no_dm_logged():
    send_dm = AsyncMock()
    with patch(_RESOLVER, AsyncMock(return_value=None)):
        result = await handle_usage_command(
            normalized=_normalized("/usage", username="@stranger"),
            api_client=_api_client(),
            send_dm=send_dm,
            admin_username="@admin",
            internal_token="tok",
            web_ui_base_url="http://ui:8001",
            default_timezone="UTC",
        )
    assert result == {"status": "ignored", "reason": "unauthorized_usage"}
    send_dm.assert_not_called()


@pytest.mark.asyncio
async def test_operator_gets_formatted_reply():
    send_dm = AsyncMock()
    client = _api_client(operator=_OP, fetch_result=_USAGE_RESULT, projects=_PROJECTS)
    with patch(_RESOLVER, AsyncMock(return_value=_OP)):
        result = await handle_usage_command(
            normalized=_normalized("/usage", username="@op"),
            api_client=client,
            send_dm=send_dm,
            admin_username="@admin",
            internal_token="tok",
            web_ui_base_url="http://ui:8001",
            default_timezone="UTC",
        )
    assert result == {"status": "usage_sent", "scope": "operator"}
    send_dm.assert_awaited_once()
    sent_text = send_dm.await_args.args[1]
    # Operator output must be byte-clean
    assert "$" not in sent_text
    assert "Расход" not in sent_text


@pytest.mark.asyncio
async def test_operator_fetch_called_with_correct_args():
    send_dm = AsyncMock()
    client = _api_client(operator=_OP, fetch_result=_USAGE_RESULT, projects=_PROJECTS)
    with patch(_RESOLVER, AsyncMock(return_value=_OP)):
        await handle_usage_command(
            normalized=_normalized("/usage", username="@op"),
            api_client=client,
            send_dm=send_dm,
            admin_username="@admin",
            internal_token="tok",
            web_ui_base_url="http://ui:8001",
            default_timezone="UTC",
        )
    client.fetch_usage_today.assert_awaited_once()
    call_kwargs = client.fetch_usage_today.await_args.kwargs
    assert call_kwargs["project_id"] == 5
    assert call_kwargs["scope"] == "operator"
    assert call_kwargs["as_user"] == "@op"


@pytest.mark.asyncio
async def test_admin_gets_cost_in_reply():
    send_dm = AsyncMock()
    admin_result = {
        "summary_rows": [
            {
                "tracker_type": "llm",
                "model_name": "claude-haiku-4-5",
                "prompt_tokens_total": 1000,
                "completion_tokens_total": 500,
                "cost_usd_total": 0.10,
                "wasted_cost_usd": None,
                "call_count": 5,
                "in_count": None,
                "out_count": None,
                "hitl_created_count": None,
                "hitl_assigned_count": None,
                "hitl_replied_count": None,
                "hitl_resolved_count": None,
            }
        ],
        "wasted_rows": [{"wasted_cost_usd": 0.02}],
    }
    client = _api_client(fetch_result=admin_result, projects=_PROJECTS)
    with patch(_RESOLVER, AsyncMock(return_value=_OP)):
        result = await handle_usage_command(
            normalized=_normalized("/usage Салон", username="@admin"),
            api_client=client,
            send_dm=send_dm,
            admin_username="@admin",
            internal_token="tok",
            web_ui_base_url="http://ui:8001",
            default_timezone="UTC",
        )
    assert result == {"status": "usage_sent", "scope": "admin"}
    sent_text = send_dm.await_args.args[1]
    assert "$" in sent_text
    assert "Расход" in sent_text


@pytest.mark.asyncio
async def test_admin_with_project_name_arg():
    send_dm = AsyncMock()
    projects = [
        {"id": 5, "slug": "salon", "name": "Салон"},
        {"id": 9, "slug": "spa", "name": "Спа"},
    ]
    client = _api_client(fetch_result=_USAGE_RESULT, projects=projects)
    with patch(_RESOLVER, AsyncMock(return_value=None)):
        result = await handle_usage_command(
            normalized=_normalized("/usage Салон", username="@admin"),
            api_client=client,
            send_dm=send_dm,
            admin_username="@admin",
            internal_token="tok",
            web_ui_base_url="http://ui:8001",
            default_timezone="UTC",
        )
    assert result == {"status": "usage_sent", "scope": "admin"}
    call_kwargs = client.fetch_usage_today.await_args.kwargs
    assert call_kwargs["project_id"] == 5


@pytest.mark.asyncio
async def test_admin_unknown_project_name():
    send_dm = AsyncMock()
    client = _api_client(projects=[{"id": 5, "slug": "salon", "name": "Салон"}])
    with patch(_RESOLVER, AsyncMock(return_value=None)):
        result = await handle_usage_command(
            normalized=_normalized("/usage НеСуществует", username="@admin"),
            api_client=client,
            send_dm=send_dm,
            admin_username="@admin",
            internal_token="tok",
            web_ui_base_url="http://ui:8001",
            default_timezone="UTC",
        )
    assert result == {"status": "usage_unknown_project"}
    send_dm.assert_awaited_once()
    assert "НеСуществует" in send_dm.await_args.args[1]


@pytest.mark.asyncio
async def test_admin_no_arg_sends_project_list():
    send_dm = AsyncMock()
    projects = [{"id": 5, "slug": "salon", "name": "Салон"}]
    client = _api_client(projects=projects)
    with patch(_RESOLVER, AsyncMock(return_value=None)):
        result = await handle_usage_command(
            normalized=_normalized("/usage", username="@admin"),
            api_client=client,
            send_dm=send_dm,
            admin_username="@admin",
            internal_token="tok",
            web_ui_base_url="http://ui:8001",
            default_timezone="UTC",
        )
    assert result == {"status": "usage_admin_specify_project"}
    send_dm.assert_awaited_once()
    msg = send_dm.await_args.args[1]
    assert "/usage" in msg
    assert "Салон" in msg


@pytest.mark.asyncio
async def test_operator_with_no_project_assignment():
    send_dm = AsyncMock()
    op_no_proj = ResolvedOperator(
        username="@op", chat_id=100, project_id=None, is_active=True, source="registry"
    )
    with patch(_RESOLVER, AsyncMock(return_value=op_no_proj)):
        result = await handle_usage_command(
            normalized=_normalized("/usage", username="@op"),
            api_client=_api_client(),
            send_dm=send_dm,
            admin_username="@admin",
            internal_token="tok",
            web_ui_base_url="http://ui:8001",
            default_timezone="UTC",
        )
    assert result == {"status": "usage_no_project"}
    send_dm.assert_awaited_once()
    assert "проект" in send_dm.await_args.args[1].lower()


@pytest.mark.asyncio
async def test_degraded_api_sends_degraded_message():
    send_dm = AsyncMock()
    client = _api_client(operator=_OP, fetch_result=None, projects=_PROJECTS)
    with patch(_RESOLVER, AsyncMock(return_value=_OP)):
        result = await handle_usage_command(
            normalized=_normalized("/usage", username="@op"),
            api_client=client,
            send_dm=send_dm,
            admin_username="@admin",
            internal_token="tok",
            web_ui_base_url="http://ui:8001",
            default_timezone="UTC",
        )
    assert result == {"status": "usage_degraded"}
    send_dm.assert_awaited_once()
    assert "недоступны" in send_dm.await_args.args[1]


@pytest.mark.asyncio
async def test_resolve_project_by_name_api_exception_treated_as_unknown():
    """_resolve_project_by_name exception → returns (None, None) → usage_unknown_project."""
    send_dm = AsyncMock()
    client = _api_client()
    client.list_projects = AsyncMock(side_effect=RuntimeError("api down"))
    with patch(_RESOLVER, AsyncMock(return_value=None)):
        result = await handle_usage_command(
            normalized=_normalized("/usage НеСуществует", username="@admin"),
            api_client=client,
            send_dm=send_dm,
            admin_username="@admin",
            internal_token="tok",
            web_ui_base_url="http://ui:8001",
            default_timezone="UTC",
        )
    assert result == {"status": "usage_unknown_project"}


@pytest.mark.asyncio
async def test_get_project_name_api_exception_falls_back_to_id():
    """_get_project_name exception → falls back to str(project_id) in the text."""
    send_dm = AsyncMock()
    op_no_name = ResolvedOperator(
        username="@op", chat_id=100, project_id=42, is_active=True, source="registry"
    )
    client = _api_client(fetch_result=_USAGE_RESULT)
    client.list_projects = AsyncMock(side_effect=RuntimeError("api down"))
    with patch(_RESOLVER, AsyncMock(return_value=op_no_name)):
        result = await handle_usage_command(
            normalized=_normalized("/usage", username="@op"),
            api_client=client,
            send_dm=send_dm,
            admin_username="@admin",
            internal_token="tok",
            web_ui_base_url="http://ui:8001",
            default_timezone="UTC",
        )
    assert result == {"status": "usage_sent", "scope": "operator"}
    sent_text = send_dm.await_args.args[1]
    assert "42" in sent_text


@pytest.mark.asyncio
async def test_get_project_name_not_found_falls_back_to_id():
    """_get_project_name finds no match → falls back to str(project_id)."""
    send_dm = AsyncMock()
    op_no_match = ResolvedOperator(
        username="@op", chat_id=100, project_id=99, is_active=True, source="registry"
    )
    client = _api_client(
        fetch_result=_USAGE_RESULT,
        projects=[{"id": 5, "slug": "salon", "name": "Салон"}],
    )
    with patch(_RESOLVER, AsyncMock(return_value=op_no_match)):
        result = await handle_usage_command(
            normalized=_normalized("/usage", username="@op"),
            api_client=client,
            send_dm=send_dm,
            admin_username="@admin",
            internal_token="tok",
            web_ui_base_url="http://ui:8001",
            default_timezone="UTC",
        )
    assert result == {"status": "usage_sent", "scope": "operator"}
    sent_text = send_dm.await_args.args[1]
    assert "99" in sent_text
