"""Sticky inbound routing: re-assign to the previous operator when active."""

from __future__ import annotations

import pytest

from services.api.app import main as api_main
from services.api.app.hitl import HitlTicketRepository
from services.api.app.operators import OperatorRepository
from services.api.app.projects import ProjectRepository


@pytest.fixture
def stack(tmp_path, monkeypatch):
    projects = ProjectRepository(str(tmp_path / "projects.sqlite3"))
    operators = OperatorRepository(str(tmp_path / "operators.sqlite3"))
    hitl = HitlTicketRepository(str(tmp_path / "hitl.sqlite3"))
    default = projects.ensure_default_project()
    operators.create(username="@primary", project_id=default.id)
    monkeypatch.setattr(api_main, "project_repository", projects)
    monkeypatch.setattr(api_main, "operator_repository", operators)
    monkeypatch.setattr(api_main, "hitl_ticket_repository", hitl)
    return {
        "projects": projects,
        "operators": operators,
        "hitl": hitl,
        "default": default,
    }


def test_no_prior_ticket_picks_primary(stack):
    assert api_main._pick_assignee_for_chat(123) == "@primary"


def test_chat_id_none_picks_primary(stack):
    assert api_main._pick_assignee_for_chat(None) == "@primary"


def test_prior_ticket_with_active_operator_sticks(stack):
    stack["operators"].create(
        username="@op-b", project_id=stack["default"].id
    )
    ticket = stack["hitl"].create(
        conversation_ref="conv", reason="r", target_chat_id=42
    )
    stack["hitl"].assign(ticket_id=ticket.id, operator_username="@op-b")
    assert api_main._pick_assignee_for_chat(42) == "@op-b"


def test_prior_ticket_with_inactive_operator_falls_back_to_primary(stack):
    stack["operators"].create(
        username="@op-b", project_id=stack["default"].id
    )
    stack["operators"].update(username="@op-b", is_active=False)
    ticket = stack["hitl"].create(
        conversation_ref="conv", reason="r", target_chat_id=42
    )
    stack["hitl"].assign(ticket_id=ticket.id, operator_username="@op-b")
    assert api_main._pick_assignee_for_chat(42) == "@primary"


def test_prior_ticket_with_unknown_operator_falls_back_to_primary(stack):
    ticket = stack["hitl"].create(
        conversation_ref="conv", reason="r", target_chat_id=42
    )
    stack["hitl"].assign(ticket_id=ticket.id, operator_username="@ghost")
    assert api_main._pick_assignee_for_chat(42) == "@primary"


def test_prior_ticket_assigned_to_primary_keeps_primary(stack):
    ticket = stack["hitl"].create(
        conversation_ref="conv", reason="r", target_chat_id=42
    )
    stack["hitl"].assign(ticket_id=ticket.id, operator_username="@primary")
    assert api_main._pick_assignee_for_chat(42) == "@primary"


def test_legacy_hitl_repo_falls_back(monkeypatch, stack):
    """A legacy HitlTicketRepository without latest_for_chat returns the primary."""

    class LegacyHitl:
        def get_runtime_config(self, key):
            return None

        def latest_for_chat(self, chat_id):  # pragma: no cover - replaced below
            return None

    legacy = LegacyHitl()
    # Drop `latest_for_chat` to simulate the pre-10.06 schema.
    monkeypatch.delattr(LegacyHitl, "latest_for_chat", raising=False)
    monkeypatch.setattr(api_main, "hitl_ticket_repository", legacy)
    assert api_main._pick_assignee_for_chat(1) == "@primary"


def test_hitl_latest_for_chat_returns_none_when_empty(stack):
    """The new helper handles "no tickets" cleanly."""
    assert stack["hitl"].latest_for_chat(9999) is None
