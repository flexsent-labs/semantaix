"""Tests for UsageRecorder (Story 14.02).

Fire-and-forget async queue with drop-oldest overflow and consumer dispatching
to the correct repository via asyncio.to_thread.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.api.app.usage.recorder import UsageRecorder
from services.api.app.usage.repositories import (
    UsageHitlEventRepository,
    UsageLlmCallRepository,
    UsageMessageRepository,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_recorder(
    *,
    llm_repo=None,
    message_repo=None,
    hitl_repo=None,
    queue_maxsize: int = 8,
    clock=None,
) -> UsageRecorder:
    llm_repo = llm_repo or MagicMock(spec=UsageLlmCallRepository)
    message_repo = message_repo or MagicMock(spec=UsageMessageRepository)
    hitl_repo = hitl_repo or MagicMock(spec=UsageHitlEventRepository)
    return UsageRecorder(
        llm_repo=llm_repo,
        message_repo=message_repo,
        hitl_repo=hitl_repo,
        queue_maxsize=queue_maxsize,
        clock=clock,
    )


_LLM_PAYLOAD = {
    "model_name": "gpt-4o",
    "prompt_tokens": 10,
    "completion_tokens": 20,
    "cost_usd": 0.003,
    "call_outcome": "customer_visible_answer",
    "trace_id": "t1",
    "created_at": "2026-06-11T00:00:00Z",
}


# ---------------------------------------------------------------------------
# record() enqueues without awaiting the write
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_returns_immediately_without_repo_call():
    """record() returns before the consumer has a chance to call the repo."""
    repo = MagicMock(spec=UsageLlmCallRepository)
    recorder = _make_recorder(llm_repo=repo, queue_maxsize=64)
    recorder.start()
    try:
        # Pause the consumer by replacing the queue with a new one that we
        # control — simpler: just verify repo not called synchronously.
        await recorder.record(tracker_type="llm", project_id=1, payload=_LLM_PAYLOAD)
        # Without draining, the repo call hasn't happened yet because the
        # consumer coroutine hasn't been scheduled in this tight synchronous
        # sequence. Yield once to let the event loop tick.
        # (Don't drain — that would trivially confirm it works.)
        repo.record.assert_not_called()
    finally:
        await recorder.aclose()


@pytest.mark.asyncio
async def test_consumer_dispatches_to_llm_repo():
    """After draining, the LLM repo receives the correct row."""
    repo = MagicMock(spec=UsageLlmCallRepository)
    recorder = _make_recorder(llm_repo=repo)
    recorder.start()
    await recorder.record(tracker_type="llm", project_id=1, payload=_LLM_PAYLOAD)
    await recorder.aclose()
    repo.record.assert_called_once()
    row = repo.record.call_args[0][0]
    assert row.project_id == 1
    assert row.model_name == "gpt-4o"
    assert row.prompt_tokens == 10
    assert row.completion_tokens == 20
    assert row.cost_usd == 0.003
    assert row.call_outcome == "customer_visible_answer"
    assert row.trace_id == "t1"


@pytest.mark.asyncio
async def test_consumer_dispatches_to_message_repo():
    repo = MagicMock(spec=UsageMessageRepository)
    recorder = _make_recorder(message_repo=repo)
    recorder.start()
    await recorder.record(
        tracker_type="messages",
        project_id=2,
        payload={
            "direction": "in",
            "participant_role": "customer",
            "trace_id": None,
            "created_at": "2026-06-11T00:00:00Z",
        },
    )
    await recorder.aclose()
    repo.record.assert_called_once()
    row = repo.record.call_args[0][0]
    assert row.project_id == 2
    assert row.direction == "in"


@pytest.mark.asyncio
async def test_consumer_dispatches_to_hitl_repo():
    repo = MagicMock(spec=UsageHitlEventRepository)
    recorder = _make_recorder(hitl_repo=repo)
    recorder.start()
    await recorder.record(
        tracker_type="hitl",
        project_id=3,
        payload={
            "event_type": "created",
            "ticket_id": 42,
            "trace_id": None,
            "created_at": "2026-06-11T00:00:00Z",
        },
    )
    await recorder.aclose()
    repo.record.assert_called_once()
    row = repo.record.call_args[0][0]
    assert row.project_id == 3
    assert row.event_type == "created"
    assert row.ticket_id == 42


# ---------------------------------------------------------------------------
# Consumer error handling — loop continues on exception
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_consumer_logs_and_continues_after_repo_error(caplog):
    """If repo.record raises, the consumer logs usage_record_failed and keeps going."""
    repo = MagicMock(spec=UsageLlmCallRepository)
    repo.record.side_effect = [RuntimeError("db locked"), None]
    recorder = _make_recorder(llm_repo=repo, queue_maxsize=8)
    recorder.start()

    with caplog.at_level(logging.WARNING, logger="services.api.app.usage.recorder"):
        await recorder.record(tracker_type="llm", project_id=1, payload=_LLM_PAYLOAD)
        await recorder.record(tracker_type="llm", project_id=1, payload=_LLM_PAYLOAD)
        await recorder.aclose()

    assert any("usage_record_failed" in r.message for r in caplog.records)
    assert repo.record.call_count == 2  # second call succeeded


# ---------------------------------------------------------------------------
# Queue overflow — drop-oldest
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drop_oldest_on_overflow(caplog):
    """When the queue is full, the oldest item is dropped and the newest enqueued."""
    # Use a very small queue and pause the consumer task so it never drains
    maxsize = 2
    # We'll create the recorder but NOT call start() so the consumer never runs.
    repo = MagicMock(spec=UsageLlmCallRepository)
    recorder = _make_recorder(llm_repo=repo, queue_maxsize=maxsize)
    # Don't start — fill the queue manually
    # Fill to maxsize
    payload_a = dict(_LLM_PAYLOAD, trace_id="a")
    payload_b = dict(_LLM_PAYLOAD, trace_id="b")
    payload_c = dict(_LLM_PAYLOAD, trace_id="c")

    await recorder.record(tracker_type="llm", project_id=1, payload=payload_a)
    await recorder.record(tracker_type="llm", project_id=1, payload=payload_b)
    # Queue is full. Next record() should drop oldest ("a") and enqueue "c".
    with caplog.at_level(logging.WARNING, logger="services.api.app.usage.recorder"):
        await recorder.record(tracker_type="llm", project_id=1, payload=payload_c)

    assert any("usage_queue_overflow_drop" in r.message for r in caplog.records)
    # Queue should contain b and c (a was dropped)
    assert recorder._queue.qsize() == maxsize
    item_1 = recorder._queue.get_nowait()
    item_2 = recorder._queue.get_nowait()
    assert item_1["payload"]["trace_id"] == "b"
    assert item_2["payload"]["trace_id"] == "c"


# ---------------------------------------------------------------------------
# aclose() — drains then stops
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_aclose_drains_all_items():
    """aclose() waits for all queued items to be processed."""
    repo = MagicMock(spec=UsageLlmCallRepository)
    recorder = _make_recorder(llm_repo=repo, queue_maxsize=16)
    recorder.start()
    for i in range(5):
        await recorder.record(
            tracker_type="llm", project_id=1,
            payload=dict(_LLM_PAYLOAD, trace_id=f"t{i}"),
        )
    await recorder.aclose()
    assert repo.record.call_count == 5


@pytest.mark.asyncio
async def test_aclose_emits_stopped_log(caplog):
    recorder = _make_recorder()
    recorder.start()
    with caplog.at_level(logging.INFO, logger="services.api.app.usage.recorder"):
        await recorder.aclose()
    assert any("usage_recorder_stopped" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Post-close record() raises RuntimeError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_raises_after_aclose():
    recorder = _make_recorder()
    recorder.start()
    await recorder.aclose()
    with pytest.raises(RuntimeError):
        await recorder.record(tracker_type="llm", project_id=1, payload=_LLM_PAYLOAD)


# ---------------------------------------------------------------------------
# Invalid tracker_type
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_raises_on_invalid_tracker_type():
    recorder = _make_recorder()
    with pytest.raises(ValueError, match="Unknown tracker_type"):
        await recorder.record(tracker_type="bogus", project_id=1, payload={})


# ---------------------------------------------------------------------------
# start() emits structured log
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_emits_started_log(caplog):
    recorder = _make_recorder()
    with caplog.at_level(logging.INFO, logger="services.api.app.usage.recorder"):
        recorder.start()
    await recorder.aclose()
    assert any("usage_recorder_started" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Frozen clock — created_at injected correctly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_frozen_clock_used_for_created_at():
    """The recorder should pass the payload's created_at through unchanged.
    (The clock is used by the OpenRouter client to generate created_at;
    the recorder simply dispatches whatever is in the payload.)"""
    repo = MagicMock(spec=UsageLlmCallRepository)
    fixed_ts = "2099-01-01T00:00:00Z"
    recorder = _make_recorder(llm_repo=repo)
    recorder.start()
    await recorder.record(
        tracker_type="llm",
        project_id=1,
        payload=dict(_LLM_PAYLOAD, created_at=fixed_ts),
    )
    await recorder.aclose()
    row = repo.record.call_args[0][0]
    assert row.created_at == fixed_ts


# ---------------------------------------------------------------------------
# QueueEmpty guard in overflow path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_overflow_queue_empty_exception_is_silenced():
    """If get_nowait() raises QueueEmpty during overflow, the error is swallowed.

    This exercises the defensive guard for the race condition where the consumer
    drains an item between the full() check and the get_nowait() call.  We use a
    fully-mocked queue so put() is non-blocking even though full() returns True.
    """
    repo = MagicMock(spec=UsageLlmCallRepository)
    recorder = _make_recorder(llm_repo=repo, queue_maxsize=4)
    # Replace _queue with a mock that simulates the race condition without
    # causing asyncio.Queue.put() to block on the patched full().
    mock_queue = MagicMock(spec=asyncio.Queue)
    mock_queue.full.return_value = True
    mock_queue.get_nowait.side_effect = asyncio.QueueEmpty
    mock_queue.put = AsyncMock()
    recorder._queue = mock_queue

    # Should not raise; the except clause swallows QueueEmpty
    await recorder.record(tracker_type="llm", project_id=1, payload=_LLM_PAYLOAD)
    mock_queue.put.assert_called_once()
