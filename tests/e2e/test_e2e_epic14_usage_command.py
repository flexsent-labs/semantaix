"""E2E tests for /usage bot command — Story 14.08."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import services.bot_gateway.app.main as bot_main
from services.api.app.operators import OperatorRepository
from services.api.app.projects import ProjectRepository
from services.bot_gateway.app.operator_resolver import ResolvedOperator
from services.bot_gateway.app.webhook_dedup import WebhookUpdateClaimRepository

pytestmark = [pytest.mark.e2e, pytest.mark.epic("14")]

_OP_RESOLVED = ResolvedOperator(
    username="@op-e2e", chat_id=555, project_id=10, is_active=True, source="registry"
)
_SUMMARY_ROWS = [
    {
        "tracker_type": "llm",
        "model_name": "claude-haiku-4-5",
        "prompt_tokens_total": 500,
        "completion_tokens_total": 200,
        "cost_usd_total": 0.02,
        "wasted_cost_usd": 0.005,
        "call_count": 2,
        "in_count": None,
        "out_count": None,
        "hitl_created_count": None,
        "hitl_assigned_count": None,
        "hitl_replied_count": None,
        "hitl_resolved_count": None,
    }
]
_WASTED_ROWS = [{"wasted_cost_usd": 0.005}]
_PROJECTS = [{"id": 10, "slug": "e2e-project", "name": "E2E проект"}]


def _payload(update_id: int, username: str, text: str, chat_id: int = 555) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": chat_id, "username": username.lstrip("@")},
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
        },
    }


@pytest.fixture()
def bot_client(tmp_path, monkeypatch):
    proj = ProjectRepository(str(tmp_path / "proj.db"))
    ops = OperatorRepository(str(tmp_path / "ops.db"))
    dedup = WebhookUpdateClaimRepository(str(tmp_path / "dedup.db"))
    default = proj.ensure_default_project()
    ops.create(username="@op-e2e", project_id=default.id, chat_id=555)

    monkeypatch.setattr(bot_main, "_project_repository", proj)
    monkeypatch.setattr(bot_main, "webhook_update_claim_repository", dedup)
    monkeypatch.setattr(bot_main.settings, "telegram_bot_token", "stub-token")
    monkeypatch.setattr(bot_main.settings, "admin_telegram_username", "@admin-e2e")
    monkeypatch.setattr(bot_main.settings, "hitl_config_admin_username", "@admin-e2e")
    monkeypatch.setattr(bot_main.settings, "web_ui_base_url", "http://ui:8001")
    monkeypatch.setattr(bot_main.settings, "internal_service_token", "e2e-internal")

    return TestClient(bot_main.app)


@pytest.mark.story("14-08")
@pytest.mark.asyncio
async def test_operator_usage_dm_has_no_dollar_sign(bot_client, monkeypatch):
    """Operator /usage DM must not expose cost data."""
    dms: list[tuple] = []

    async def capture_dm(chat_id, text, **_kw):
        dms.append((chat_id, text))

    monkeypatch.setattr(bot_main, "_send_dm", capture_dm)

    with (
        patch(
            "services.bot_gateway.app.usage_command.resolve_operator_for_sender",
            AsyncMock(return_value=_OP_RESOLVED),
        ),
        patch.object(
            bot_main.api_client, "fetch_usage_today",
            AsyncMock(return_value={"summary_rows": _SUMMARY_ROWS, "wasted_rows": None}),
        ),
        patch.object(
            bot_main.api_client, "list_projects",
            AsyncMock(return_value={"items": _PROJECTS}),
        ),
    ):
        resp = bot_client.post("/telegram/webhook", json=_payload(70001, "@op-e2e", "/usage"))

    assert resp.status_code == 200
    assert len(dms) == 1
    _, text = dms[0]
    assert "$" not in text
    assert "Расход" not in text
    assert "http://ui:8001/admin/usage" in text


@pytest.mark.story("14-08")
@pytest.mark.asyncio
async def test_admin_usage_dm_contains_cost(bot_client, monkeypatch):
    """Admin /usage DM must include cost and wasted-spend line."""
    dms: list[tuple] = []

    async def capture_dm(chat_id, text, **_kw):
        dms.append((chat_id, text))

    monkeypatch.setattr(bot_main, "_send_dm", capture_dm)

    with (
        patch(
            "services.bot_gateway.app.usage_command.resolve_operator_for_sender",
            AsyncMock(return_value=None),
        ),
        patch.object(
            bot_main.api_client, "fetch_usage_today",
            AsyncMock(return_value={"summary_rows": _SUMMARY_ROWS, "wasted_rows": _WASTED_ROWS}),
        ),
        patch.object(
            bot_main.api_client, "list_projects",
            AsyncMock(return_value={"items": _PROJECTS}),
        ),
    ):
        resp = bot_client.post(
            "/telegram/webhook",
            json=_payload(70002, "@admin-e2e", "/usage E2E проект", chat_id=1),
        )

    assert resp.status_code == 200
    assert len(dms) == 1
    _, text = dms[0]
    assert "$" in text
    assert "Расход" in text


@pytest.mark.story("14-08")
@pytest.mark.asyncio
async def test_admin_usage_with_project_name_arg(bot_client, monkeypatch):
    """/usage <project_name> resolves to that project."""
    dms: list[tuple] = []

    async def capture_dm(chat_id, text, **_kw):
        dms.append((chat_id, text))

    monkeypatch.setattr(bot_main, "_send_dm", capture_dm)
    mock_fetch = AsyncMock(return_value={"summary_rows": _SUMMARY_ROWS, "wasted_rows": None})

    with (
        patch(
            "services.bot_gateway.app.usage_command.resolve_operator_for_sender",
            AsyncMock(return_value=None),
        ),
        patch.object(bot_main.api_client, "fetch_usage_today", mock_fetch),
        patch.object(
            bot_main.api_client, "list_projects",
            AsyncMock(return_value={"items": _PROJECTS}),
        ),
    ):
        resp = bot_client.post(
            "/telegram/webhook",
            json=_payload(70003, "@admin-e2e", "/usage e2e-project", chat_id=1),
        )

    assert resp.status_code == 200
    call_kwargs = mock_fetch.await_args.kwargs
    assert call_kwargs["project_id"] == 10


@pytest.mark.story("14-08")
@pytest.mark.asyncio
async def test_non_registered_sender_receives_no_dm(bot_client, monkeypatch):
    """/usage from non-registered sender must not DM anything."""
    dms: list[tuple] = []

    async def capture_dm(chat_id, text, **_kw):
        dms.append((chat_id, text))

    monkeypatch.setattr(bot_main, "_send_dm", capture_dm)

    with patch(
        "services.bot_gateway.app.usage_command.resolve_operator_for_sender",
        AsyncMock(return_value=None),
    ):
        resp = bot_client.post(
            "/telegram/webhook",
            json=_payload(70004, "@stranger", "/usage"),
        )

    assert resp.status_code == 200
    assert len(dms) == 0


@pytest.mark.story("14-08")
@pytest.mark.asyncio
async def test_degraded_api_sends_degraded_message(bot_client, monkeypatch):
    """When the api is unreachable, the bot sends the degraded-state message."""
    dms: list[tuple] = []

    async def capture_dm(chat_id, text, **_kw):
        dms.append((chat_id, text))

    monkeypatch.setattr(bot_main, "_send_dm", capture_dm)

    with (
        patch(
            "services.bot_gateway.app.usage_command.resolve_operator_for_sender",
            AsyncMock(return_value=_OP_RESOLVED),
        ),
        patch.object(
            bot_main.api_client, "fetch_usage_today",
            AsyncMock(return_value=None),
        ),
        patch.object(
            bot_main.api_client, "list_projects",
            AsyncMock(return_value={"items": _PROJECTS}),
        ),
    ):
        resp = bot_client.post(
            "/telegram/webhook",
            json=_payload(70005, "@op-e2e", "/usage"),
        )

    assert resp.status_code == 200
    assert len(dms) == 1
    _, text = dms[0]
    assert "недоступны" in text
