"""Integration tests for /usage command via _process_telegram_update (Story 14.08).

Calling _process_telegram_update directly (not via TestClient) avoids thread
isolation issues with coverage tracking. The function is async so these are
async tests, exercising the actual routing path in main.py.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import services.bot_gateway.app.main as bot_main
from services.api.app.hitl import HitlTicketRepository
from services.api.app.operators import OperatorRepository
from services.api.app.projects import ProjectRepository
from services.bot_gateway.app.operator_resolver import ResolvedOperator
from services.bot_gateway.app.webhook_dedup import WebhookUpdateClaimRepository

_MAIN_RESOLVER = "services.bot_gateway.app.main.resolve_operator_for_sender"
_CMD_RESOLVER = "services.bot_gateway.app.usage_command.resolve_operator_for_sender"


def _setup(tmp_path, monkeypatch):
    proj = ProjectRepository(str(tmp_path / "proj.db"))
    ops = OperatorRepository(str(tmp_path / "ops.db"))
    hitl = HitlTicketRepository(str(tmp_path / "hitl.db"))
    dedup = WebhookUpdateClaimRepository(str(tmp_path / "dedup.db"))

    default = proj.ensure_default_project()
    ops.create(username="@op-usage", project_id=default.id, chat_id=999)

    monkeypatch.setattr(bot_main, "_project_repository", proj)
    monkeypatch.setattr(bot_main, "hitl_ticket_repository", hitl)
    monkeypatch.setattr(bot_main, "webhook_update_claim_repository", dedup)
    monkeypatch.setattr(bot_main.settings, "telegram_bot_token", "stub-token")
    monkeypatch.setattr(bot_main.settings, "admin_telegram_username", "@admin-usage")
    monkeypatch.setattr(bot_main.settings, "hitl_config_admin_username", "@admin-usage")
    monkeypatch.setattr(bot_main.settings, "web_ui_base_url", "http://ui:8001")
    monkeypatch.setattr(bot_main.settings, "internal_service_token", "test-internal")

    return default.id


def _payload(update_id: int, username: str, text: str, chat_id: int = 999) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": chat_id, "username": username.lstrip("@")},
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
        },
    }


_SAMPLE_USAGE = {
    "summary_rows": [
        {
            "tracker_type": "llm",
            "model_name": "claude-haiku-4-5",
            "prompt_tokens_total": 800,
            "completion_tokens_total": 300,
            "cost_usd_total": 0.04,
            "wasted_cost_usd": None,
            "call_count": 2,
            "in_count": None,
            "out_count": None,
            "hitl_created_count": None,
            "hitl_assigned_count": None,
            "hitl_replied_count": None,
            "hitl_resolved_count": None,
        },
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
        },
    ],
    "wasted_rows": None,
}

_ADMIN_USAGE = {
    **_SAMPLE_USAGE,
    "wasted_rows": [{"wasted_cost_usd": 0.01}],
}


@pytest.mark.asyncio
async def test_operator_usage_reply_is_byte_clean(tmp_path, monkeypatch):
    project_id = _setup(tmp_path, monkeypatch)
    op = ResolvedOperator(
        username="@op-usage", chat_id=999, project_id=project_id,
        is_active=True, source="registry",
    )
    dms_sent: list[tuple] = []

    async def capture_dm(chat_id, text, **_kw):
        dms_sent.append((chat_id, text))

    monkeypatch.setattr(bot_main, "_send_dm", capture_dm)
    bg = MagicMock()
    bg.add_task = MagicMock()

    with (
        patch(_MAIN_RESOLVER, AsyncMock(return_value=op)),
        patch(_CMD_RESOLVER, AsyncMock(return_value=op)),
        patch.object(
            bot_main.api_client, "fetch_usage_today", AsyncMock(return_value=_SAMPLE_USAGE)
        ),
        patch.object(
            bot_main.api_client, "list_projects",
            AsyncMock(return_value={
                "items": [{"id": project_id, "slug": "default", "name": "Default"}],
            }),
        ),
    ):
        result = await bot_main._process_telegram_update(
            _payload(60001, "@op-usage", "/usage"), "trace-60001", bg
        )

    assert result.get("status") == "usage_sent"
    assert result.get("scope") == "operator"
    assert len(dms_sent) == 1
    _, text = dms_sent[0]
    assert "$" not in text
    assert "Расход" not in text
    assert "Потрачено впустую" not in text
    assert "http://ui:8001/admin/usage" in text


@pytest.mark.asyncio
async def test_admin_usage_reply_contains_cost(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    dms_sent: list[tuple] = []

    async def capture_dm(chat_id, text, **_kw):
        dms_sent.append((chat_id, text))

    monkeypatch.setattr(bot_main, "_send_dm", capture_dm)
    bg = MagicMock()
    bg.add_task = MagicMock()

    with (
        patch(_MAIN_RESOLVER, AsyncMock(return_value=None)),
        patch(_CMD_RESOLVER, AsyncMock(return_value=None)),
        patch.object(
            bot_main.api_client, "fetch_usage_today", AsyncMock(return_value=_ADMIN_USAGE)
        ),
        patch.object(
            bot_main.api_client, "list_projects",
            AsyncMock(return_value={
                "items": [{"id": 1, "slug": "default", "name": "Default"}],
            }),
        ),
    ):
        result = await bot_main._process_telegram_update(
            _payload(60002, "@admin-usage", "/usage default"), "trace-60002", bg
        )

    assert result.get("status") == "usage_sent"
    assert result.get("scope") == "admin"
    assert len(dms_sent) == 1
    _, text = dms_sent[0]
    assert "$" in text
    assert "Расход" in text
