"""Epic 10.5 story 10.5-04 E2E: cross-project isolation — P1 operator never handles P2 traffic."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from services.api.app.answerers import AnswerResult

pytestmark = [pytest.mark.e2e, pytest.mark.epic("10.5")]


@pytest.mark.story("10.5-04")
@pytest.mark.asyncio
async def test_p1_operator_never_receives_p2_escalation(tmp_path, monkeypatch):
    """
    Operator @op-p1 (in project P1) must never receive a HITL ticket
    for a customer whose routing context is project P2.

    Setup:
      - P1: default project, @op-p1 (first active operator → would be the
        fallback if isolation breaks)
      - P2: secondary project, @op-p2
      - Chat 300 has a prior resolved ticket assigned to @op-p2 →
        sticky routing resolves P2 context for chat 300.

    Assertion:
      New inbound from chat 300 is assigned to @op-p2 and the project
      context is P2. @op-p1 must not appear as the assignee.
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

    p1 = proj.ensure_default_project()
    p2 = proj.create(slug="salon-p2", name="Салон P2")

    ops.create(username="@op-p1", project_id=p1.id, chat_id=101)
    op_p2 = ops.create(username="@op-p2", project_id=p2.id, chat_id=202)

    prior = hitl.create(
        conversation_ref="chat:300:prior",
        reason="customer_question",
        target_chat_id=300,
    )
    hitl.assign(ticket_id=prior.id, operator_username=op_p2.username)

    monkeypatch.setattr(api_main, "project_repository", proj)
    monkeypatch.setattr(api_main, "operator_repository", ops)
    monkeypatch.setattr(api_main, "hitl_ticket_repository", hitl)
    monkeypatch.setattr(api_main, "answer_trace_repository", traces)
    monkeypatch.setattr(api_main.settings, "telegram_bot_token", "stub-token")
    # Bypass the answer pipeline — we're testing cross-project routing, not answering.
    monkeypatch.setattr(
        api_main.answer_pipeline, "run", AsyncMock(return_value=AnswerResult(handled=False))
    )
    mock_send = AsyncMock(return_value=True)
    monkeypatch.setattr(api_main, "_safe_send_message", mock_send)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_main.app),
        base_url="http://api",
    ) as client:
        resp = await client.post(
            "/conversations/inbound",
            json={"text": "хочу записаться", "chat_id": 300, "trace_id": "e2e-isolation-10-5"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["escalated"] is True

    assignee = body["hitl_operator_username"]
    assert assignee == "@op-p2", (
        f"Expected @op-p2 (P2 operator) but got {assignee!r}. "
        "Cross-project isolation failed: P1 operator received a P2 escalation."
    )
    assert assignee != "@op-p1", (
        "@op-p1 (P1 operator) must never receive P2 escalations."
    )

    new_ticket = hitl.find_active_for_chat(300)
    assert new_ticket is not None
    assert new_ticket.operator_username == "@op-p2"


@pytest.mark.story("10.5-04")
def test_inbound_project_resolves_to_p2_via_prior_ticket(tmp_path, monkeypatch):
    """
    _resolve_inbound_project_id uses the prior ticket's operator to scope the
    project. A chat with a P2-operator ticket resolves to P2, not to P1
    (the default project).
    """
    from services.api.app import main as api_main
    from services.api.app.hitl import HitlTicketRepository
    from services.api.app.operators import OperatorRepository
    from services.api.app.projects import ProjectRepository

    proj = ProjectRepository(str(tmp_path / "proj.db"))
    ops = OperatorRepository(str(tmp_path / "ops.db"))
    hitl = HitlTicketRepository(str(tmp_path / "hitl.db"))

    p1 = proj.ensure_default_project()
    p2 = proj.create(slug="p2-scope", name="P2")

    op_p2 = ops.create(username="@op-p2-scope", project_id=p2.id, chat_id=202)

    prior = hitl.create(
        conversation_ref="chat:400:prior",
        reason="prior",
        target_chat_id=400,
    )
    hitl.assign(ticket_id=prior.id, operator_username=op_p2.username)

    monkeypatch.setattr(api_main, "project_repository", proj)
    monkeypatch.setattr(api_main, "operator_repository", ops)
    monkeypatch.setattr(api_main, "hitl_ticket_repository", hitl)

    resolved = api_main._resolve_inbound_project_id(chat_id=400)
    assert resolved == p2.id, (
        f"Expected P2 ({p2.id}) but resolved to {resolved}. "
        "Prior ticket operator should scope the project."
    )

    default_resolved = api_main._resolve_inbound_project_id(chat_id=999)
    assert default_resolved == p1.id, (
        "Unknown chat_id must fall back to the default project (P1)."
    )


@pytest.mark.story("10.5-04")
def test_operator_list_by_project_is_independent(tmp_path):
    """list_by_project_id returns only operators in that project."""
    from services.api.app.operators import OperatorRepository
    from services.api.app.projects import ProjectRepository

    proj = ProjectRepository(str(tmp_path / "proj.db"))
    ops = OperatorRepository(str(tmp_path / "ops.db"))

    p1 = proj.ensure_default_project()
    p2 = proj.create(slug="separate", name="Separate")

    ops.create(username="@only-p1", project_id=p1.id)
    ops.create(username="@only-p2", project_id=p2.id)

    p1_ops = {o.username for o in ops.list_by_project_id(p1.id)}
    p2_ops = {o.username for o in ops.list_by_project_id(p2.id)}

    assert "@only-p1" in p1_ops
    assert "@only-p2" not in p1_ops

    assert "@only-p2" in p2_ops
    assert "@only-p1" not in p2_ops
