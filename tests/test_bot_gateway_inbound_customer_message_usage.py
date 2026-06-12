"""Tests for bot_gateway inbound customer message usage recording — Story 14.03."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import services.bot_gateway.app.main as bot_main
from services.api.app.usage.recorder import UsageRecorder


@pytest.fixture
def mock_recorder():
    recorder = MagicMock(spec=UsageRecorder)
    recorder.record = AsyncMock()
    return recorder


@pytest.mark.asyncio
async def test_enqueue_inbound_customer_message_records_correct_payload(mock_recorder):
    """_enqueue_inbound_customer_message fires a record with direction=in."""
    fake_project = MagicMock()
    fake_project.id = 42

    with (
        patch.object(bot_main, "usage_recorder", mock_recorder),
        patch.object(
            bot_main._project_repository,
            "ensure_default_project",
            return_value=fake_project,
        ),
    ):
        await bot_main._enqueue_inbound_customer_message(trace_id="t-123")

    mock_recorder.record.assert_awaited_once()
    call_kwargs = mock_recorder.record.call_args.kwargs
    assert call_kwargs["tracker_type"] == "messages"
    assert call_kwargs["project_id"] == 42
    assert call_kwargs["payload"]["direction"] == "in"
    assert call_kwargs["payload"]["participant_role"] == "customer"
    assert call_kwargs["payload"]["trace_id"] == "t-123"
    assert call_kwargs["payload"]["created_at"].endswith("Z")


@pytest.mark.asyncio
async def test_enqueue_inbound_customer_message_swallows_project_lookup_error(
    mock_recorder,
):
    """Project lookup failure must not propagate — fire-and-forget."""
    with (
        patch.object(bot_main, "usage_recorder", mock_recorder),
        patch.object(
            bot_main._project_repository,
            "ensure_default_project",
            side_effect=RuntimeError("db missing"),
        ),
    ):
        # Must not raise
        await bot_main._enqueue_inbound_customer_message(trace_id="t-err")

    mock_recorder.record.assert_not_awaited()


@pytest.mark.asyncio
async def test_enqueue_inbound_customer_message_swallows_recorder_error(mock_recorder):
    """Recorder failure must not propagate — fire-and-forget."""
    fake_project = MagicMock()
    fake_project.id = 1
    mock_recorder.record.side_effect = RuntimeError("queue full")

    with (
        patch.object(bot_main, "usage_recorder", mock_recorder),
        patch.object(
            bot_main._project_repository,
            "ensure_default_project",
            return_value=fake_project,
        ),
    ):
        await bot_main._enqueue_inbound_customer_message(trace_id="t-err2")
    # No exception raised means the test passes


@pytest.mark.asyncio
async def test_enqueue_inbound_operator_message_records_correct_payload(mock_recorder):
    """_enqueue_inbound_operator_message fires direction=in, participant_role=operator."""
    fake_project = MagicMock()
    fake_project.id = 7

    with (
        patch.object(bot_main, "usage_recorder", mock_recorder),
        patch.object(
            bot_main._project_repository,
            "ensure_default_project",
            return_value=fake_project,
        ),
    ):
        await bot_main._enqueue_inbound_operator_message()

    call_kwargs = mock_recorder.record.call_args.kwargs
    assert call_kwargs["tracker_type"] == "messages"
    assert call_kwargs["project_id"] == 7
    assert call_kwargs["payload"]["direction"] == "in"
    assert call_kwargs["payload"]["participant_role"] == "operator"
    assert call_kwargs["payload"]["trace_id"] is None


@pytest.mark.asyncio
async def test_enqueue_inbound_operator_message_swallows_project_lookup_error(
    mock_recorder,
):
    """Project lookup failure must not propagate — fire-and-forget."""
    with (
        patch.object(bot_main, "usage_recorder", mock_recorder),
        patch.object(
            bot_main._project_repository,
            "ensure_default_project",
            side_effect=RuntimeError("db missing"),
        ),
    ):
        await bot_main._enqueue_inbound_operator_message()

    mock_recorder.record.assert_not_awaited()


@pytest.mark.asyncio
async def test_enqueue_inbound_operator_message_swallows_recorder_error(mock_recorder):
    """Recorder failure must not propagate — fire-and-forget."""
    fake_project = MagicMock()
    fake_project.id = 1
    mock_recorder.record.side_effect = RuntimeError("queue full")

    with (
        patch.object(bot_main, "usage_recorder", mock_recorder),
        patch.object(
            bot_main._project_repository,
            "ensure_default_project",
            return_value=fake_project,
        ),
    ):
        await bot_main._enqueue_inbound_operator_message()
    # No exception raised means the test passes
