"""Tests for api/main.py outbound customer message usage recording — Story 14.03."""
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


def test_enqueue_outbound_customer_message_skips_when_project_id_is_none(
    mock_recorder,
):
    """project_id=None must silently skip recording — no task created."""
    with patch.object(api_main, "usage_recorder", mock_recorder):
        api_main._enqueue_outbound_customer_message(project_id=None, trace_id="t-1")

    mock_recorder.record.assert_not_called()


def test_enqueue_outbound_customer_message_enqueues_when_project_id_provided(
    mock_recorder,
):
    """project_id provided → asyncio.create_task is called (recorder.record coroutine created)."""
    with (
        patch.object(api_main, "usage_recorder", mock_recorder),
        patch("services.api.app.main.asyncio") as mock_asyncio,
    ):
        api_main._enqueue_outbound_customer_message(project_id=7, trace_id="t-7")

    mock_asyncio.create_task.assert_called_once()
    call_args = mock_asyncio.create_task.call_args
    assert call_args is not None


@pytest.mark.asyncio
async def test_enqueue_outbound_customer_message_swallows_recorder_error(mock_recorder):
    """Recorder errors inside the fire-and-forget task do not propagate."""
    import asyncio

    mock_recorder.record.side_effect = RuntimeError("recorder closed")
    with patch.object(api_main, "usage_recorder", mock_recorder):
        api_main._enqueue_outbound_customer_message(project_id=5, trace_id="t-err")
        # Yield to let the fire-and-forget task execute; must not raise
        await asyncio.sleep(0)
