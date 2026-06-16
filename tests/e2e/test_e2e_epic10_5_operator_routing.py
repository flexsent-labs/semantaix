"""Epic 10.5 story 10.5-02 E2E: second operator receives HITL escalations via sticky routing."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from services.api.app.answerers import AnswerResult

pytestmark = [pytest.mark.e2e, pytest.mark.epic("10.5")]


def _make_transport(api_main):
    return httpx.ASGITransport(app=api_main.app)


@pytest.mark.story("10.5-02")
@pytest.mark.asyncio
async def test_second_operator_receives_escalation_after_sticky_routing(
    tmp_path, monkeypatch
):
    """
    Sticky routing ensures a second operator (op-b) takes over escalations for
    a chat once they've handled a ticket for that chat.

    Flow:
    1. Seed operators: @op-a in default project, @op-b in default project.
    2. Seed a prior ticket for chat 200 assigned to @op-b (resolved — sticky
       routing looks at *latest* ticket, not active).
    3. POST /conversations/inbound from chat 200 → new ticket must be assigned
       to @op-b, not @op-a (which is listed first).
    """
    from services.api.app import main as api_main
    from services.api.app.answer_trace import AnswerTraceRepository
    from services.api.app.hitl import HitlTicketRepository
    from services.api.app.operators import OperatorRepository
    from services.api.app.projects import ProjectRepository

    proj = ProjectRepository(str(tmp_path / "proj.db"))
    ops = OperatorRepository(str(tmp_path / "ops.db"))
    hitl = HitlTicketRepository(str(tmp_path / "hitl.db"))
    traces = AnswerTraceRepository(db_path=str(tmp_path / "traces.db"))
    default = proj.ensure_default_project()

    ops.create(username="@op-a", project_id=default.id, chat_id=101)
    op_b = ops.create(username="@op-b", project_id=default.id, chat_id=102)

    prior_ticket = hitl.create(
        conversation_ref="chat:200:prior", reason="customer_question", target_chat_id=200
    )
    hitl.assign(ticket_id=prior_ticket.id, operator_username=op_b.username)

    monkeypatch.setattr(api_main, "project_repository", proj)
    monkeypatch.setattr(api_main, "operator_repository", ops)
    monkeypatch.setattr(api_main, "hitl_ticket_repository", hitl)
    monkeypatch.setattr(api_main, "answer_trace_repository", traces)
    monkeypatch.setattr(api_main.settings, "telegram_bot_token", "stub-token")
    # Bypass the full answer pipeline — we're testing routing, not answering.
    monkeypatch.setattr(
        api_main.answer_pipeline, "run", AsyncMock(return_value=AnswerResult(handled=False))
    )

    mock_send = AsyncMock(return_value=True)
    monkeypatch.setattr(api_main, "_safe_send_message", mock_send)

    async with httpx.AsyncClient(
        transport=_make_transport(api_main), base_url="http://api"
    ) as client:
        resp = await client.post(
            "/conversations/inbound",
            json={"text": "вопрос", "chat_id": 200, "trace_id": "e2e-routing-10-5"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["escalated"] is True
    assert body["hitl_operator_username"] == "@op-b", (
        f"Expected @op-b (sticky) but got {body['hitl_operator_username']!r}. "
        "Sticky routing should prefer the operator from the last ticket."
    )

    new_ticket = hitl.find_active_for_chat(200)
    assert new_ticket is not None
    assert new_ticket.operator_username == "@op-b"

    op_a_ticket = hitl.find_active_for_chat(101)
    assert op_a_ticket is None, "@op-a must not receive this escalation"


@pytest.mark.story("10.5-02")
def test_operator_registry_resolves_both_operators(tmp_path):
    """Both operators are returned by list_active(); flat resolver finds each."""
    from services.api.app.operators import OperatorRepository
    from services.api.app.projects import ProjectRepository

    proj = ProjectRepository(str(tmp_path / "proj.db"))
    ops = OperatorRepository(str(tmp_path / "ops.db"))
    default = proj.ensure_default_project()

    ops.create(username="@op-a", project_id=default.id, chat_id=101)
    ops.create(username="@op-b", project_id=default.id, chat_id=102)

    active = ops.list_active()
    assert {o.username for o in active} == {"@op-a", "@op-b"}
    assert ops.find_by_username("@op-a") is not None
    assert ops.find_by_username("@op-b") is not None
