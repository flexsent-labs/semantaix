"""Tests for HITL ticket lifecycle usage instrumentation — Story 14.04.

Verifies that _enqueue_hitl_event fires for each lifecycle transition
and that recorder failures never block the HITL state machine (NFR-8).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import services.api.app.main as api_main
from services.api.app.usage.recorder import UsageRecorder


@pytest.fixture
def mock_recorder():
    recorder = MagicMock(spec=UsageRecorder)
    recorder.record = AsyncMock()
    return recorder


# ---------------------------------------------------------------------------
# _enqueue_hitl_event helper
# ---------------------------------------------------------------------------

class TestEnqueueHitlEvent:
    def test_skips_when_project_id_is_none(self, mock_recorder):
        with patch.object(api_main, "usage_recorder", mock_recorder):
            api_main._enqueue_hitl_event(
                project_id=None, event_type="created", ticket_id=1, trace_id="t-1"
            )
        mock_recorder.record.assert_not_called()

    def test_enqueues_create_task_when_project_id_provided(self, mock_recorder):
        with (
            patch.object(api_main, "usage_recorder", mock_recorder),
            patch("services.api.app.main.asyncio") as mock_asyncio,
        ):
            api_main._enqueue_hitl_event(
                project_id=7, event_type="created", ticket_id=5, trace_id="t-7"
            )
        mock_asyncio.create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_swallows_recorder_error(self, mock_recorder):
        import asyncio

        mock_recorder.record.side_effect = RuntimeError("recorder closed")
        with patch.object(api_main, "usage_recorder", mock_recorder):
            api_main._enqueue_hitl_event(
                project_id=5, event_type="created", ticket_id=2, trace_id=None
            )
            await asyncio.sleep(0)
        # No exception propagated — recorder error is swallowed

    @pytest.mark.asyncio
    async def test_payload_contains_correct_fields(self, mock_recorder):
        import asyncio

        with patch.object(api_main, "usage_recorder", mock_recorder):
            api_main._enqueue_hitl_event(
                project_id=3, event_type="assigned", ticket_id=99, trace_id="t-trace"
            )
            await asyncio.sleep(0)

        mock_recorder.record.assert_called_once()
        kw = mock_recorder.record.call_args[1]
        assert kw["tracker_type"] == "hitl"
        assert kw["project_id"] == 3
        assert kw["payload"]["event_type"] == "assigned"
        assert kw["payload"]["ticket_id"] == 99
        assert kw["trace_id"] == "t-trace"

    @pytest.mark.asyncio
    async def test_all_four_event_types_accepted(self, mock_recorder):
        import asyncio

        for event_type in ("created", "assigned", "replied", "resolved"):
            with patch.object(api_main, "usage_recorder", mock_recorder):
                api_main._enqueue_hitl_event(
                    project_id=1, event_type=event_type, ticket_id=1, trace_id=None
                )
                await asyncio.sleep(0)

        assert mock_recorder.record.call_count == 4


# ---------------------------------------------------------------------------
# deliver_hitl_ticket_reply fires replied BEFORE resolved
# ---------------------------------------------------------------------------

class TestDeliverHitlReplyEnqueuesEvents:
    @pytest.mark.asyncio
    async def test_reply_fires_replied_then_resolved(self):
        """_enqueue_hitl_event is called with replied, then resolved (in order)."""
        import asyncio

        calls: list[str] = []

        def _fake_enqueue(**kwargs):
            calls.append(kwargs["event_type"])

        fake_ticket = MagicMock()
        fake_ticket.operator_username = "op"
        fake_ticket.target_chat_id = 100
        fake_ticket.id = 7
        fake_ticket.status = "resolved"
        fake_ticket.resolved_at = "2026-06-12T10:00:00Z"

        with (
            patch.object(api_main, "_enqueue_hitl_event", side_effect=_fake_enqueue),
            patch.object(api_main, "hitl_ticket_repository") as mock_repo,
            patch.object(api_main, "telegram_bot_sender") as mock_sender,
            patch.object(api_main, "_default_project_id", return_value=1),
        ):
            mock_repo.get.return_value = fake_ticket
            mock_repo.resolve.return_value = fake_ticket
            mock_sender.send_message = AsyncMock(return_value=42)

            request = MagicMock()
            request.operator_username = "op"
            request.reply_text = "here is your answer"

            await api_main.deliver_hitl_ticket_reply(ticket_id=7, request=request)
            await asyncio.sleep(0)

        assert calls == ["replied", "resolved"]

    @pytest.mark.asyncio
    async def test_reply_uses_ticket_id_not_route_param(self):
        """event rows carry the correct ticket_id."""
        ticket_ids: list[int] = []

        def _fake_enqueue(**kwargs):
            ticket_ids.append(kwargs["ticket_id"])

        fake_ticket = MagicMock()
        fake_ticket.operator_username = "op"
        fake_ticket.target_chat_id = 55
        fake_ticket.id = 12
        fake_ticket.status = "resolved"
        fake_ticket.resolved_at = "2026-06-12T10:00:00Z"

        with (
            patch.object(api_main, "_enqueue_hitl_event", side_effect=_fake_enqueue),
            patch.object(api_main, "hitl_ticket_repository") as mock_repo,
            patch.object(api_main, "telegram_bot_sender") as mock_sender,
            patch.object(api_main, "_default_project_id", return_value=1),
        ):
            mock_repo.get.return_value = fake_ticket
            mock_repo.resolve.return_value = fake_ticket
            mock_sender.send_message = AsyncMock(return_value=99)

            request = MagicMock()
            request.operator_username = "op"
            request.reply_text = "answer"

            await api_main.deliver_hitl_ticket_reply(ticket_id=12, request=request)

        assert ticket_ids == [12, 12]


# ---------------------------------------------------------------------------
# route_hitl_ticket fires assigned event
# ---------------------------------------------------------------------------

class TestRouteHitlTicketEnqueuesEvent:
    @pytest.mark.asyncio
    async def test_route_fires_assigned_event(self):
        calls: list[str] = []

        def _fake_enqueue(**kwargs):
            calls.append(kwargs["event_type"])

        fake_ticket = MagicMock()
        fake_ticket.id = 5
        fake_ticket.status = "assigned"
        fake_ticket.operator_username = "op2"

        with (
            patch.object(api_main, "_enqueue_hitl_event", side_effect=_fake_enqueue),
            patch.object(api_main, "hitl_ticket_repository") as mock_repo,
            patch.object(
                api_main, "_effective_hitl_operator_username", return_value="op2"
            ),
            patch.object(api_main, "_default_project_id", return_value=1),
            patch.object(
                api_main,
                "_notify_hitl_operator_summary",
                new=AsyncMock(return_value=True),
            ),
        ):
            mock_repo.assign.return_value = fake_ticket
            request = MagicMock()
            request.operator_username = "op2"

            await api_main.route_hitl_ticket(ticket_id=5, request=request)

        assert calls == ["assigned"]


# ---------------------------------------------------------------------------
# resolve_hitl_ticket fires resolved event
# ---------------------------------------------------------------------------

class TestResolveHitlTicketEnqueuesEvent:
    @pytest.mark.asyncio
    async def test_resolve_fires_resolved_event(self):
        calls: list[str] = []

        def _fake_enqueue(**kwargs):
            calls.append(kwargs["event_type"])

        fake_ticket = MagicMock()
        fake_ticket.id = 9
        fake_ticket.status = "resolved"
        fake_ticket.resolved_at = "2026-06-12T11:00:00Z"

        with (
            patch.object(api_main, "_enqueue_hitl_event", side_effect=_fake_enqueue),
            patch.object(api_main, "hitl_ticket_repository") as mock_repo,
            patch.object(api_main, "_default_project_id", return_value=1),
        ):
            mock_repo.resolve.return_value = fake_ticket

            await api_main.resolve_hitl_ticket(ticket_id=9)

        assert calls == ["resolved"]
