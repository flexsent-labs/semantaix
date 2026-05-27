"""Tests for the scheduler service module hooks and defaults.

Covers the default clock, default project-tz lookup, and the FastAPI
startup/shutdown hooks (`_start_runner`, `_stop_runner`) — including the
re-entrancy guard and the wait_for-timeout cancel branch.
"""

from __future__ import annotations

import asyncio as real_asyncio
from datetime import UTC, datetime

import pytest


def test_default_clock_returns_aware_utc_datetime() -> None:
    from services.scheduler.app.main import _default_clock

    now = _default_clock()
    assert now.tzinfo is UTC
    # Sanity check: clock is wall-clock-recent.
    assert abs((now - datetime.now(UTC)).total_seconds()) < 5.0


def test_default_project_tz_lookup_returns_settings_default() -> None:
    from platform_common.settings import get_settings
    from services.scheduler.app.main import _default_project_tz_lookup

    expected = get_settings().default_timezone
    assert _default_project_tz_lookup(42) == expected
    # Project id is ignored; multiple projects share the platform default.
    assert _default_project_tz_lookup(99) == expected


class _QuickStoppingRunner:
    """Stand-in for ``SchedulerRunner`` that exits when ``request_stop`` fires."""

    def __init__(self) -> None:
        self._stop = real_asyncio.Event()
        self.ticks = 0

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            self.ticks += 1
            try:
                await real_asyncio.wait_for(self._stop.wait(), timeout=0.01)
            except real_asyncio.TimeoutError:
                continue

    def request_stop(self) -> None:
        self._stop.set()


@pytest.mark.asyncio
async def test_start_runner_creates_task_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.scheduler.app.main as scheduler_main

    fake_runner = _QuickStoppingRunner()
    monkeypatch.setattr(scheduler_main, "runner", fake_runner)
    monkeypatch.setattr(scheduler_main, "_runner_task", None)

    try:
        await scheduler_main._start_runner()
        first_task = scheduler_main._runner_task
        assert first_task is not None
        assert not first_task.done()

        # Let the runner take at least one tick so the body actually executes.
        await real_asyncio.sleep(0.05)

        # A second startup-hook call while the task is alive is a no-op.
        await scheduler_main._start_runner()
        assert scheduler_main._runner_task is first_task

        await scheduler_main._stop_runner()
        assert scheduler_main._runner_task is None
        assert first_task.done()
        assert fake_runner.ticks >= 1
    finally:
        monkeypatch.setattr(scheduler_main, "_runner_task", None)


class _HungRunner:
    """Runner whose ``run_forever`` never returns; ``request_stop`` is a no-op."""

    async def run_forever(self) -> None:
        # Long sleep so wait_for must time out to exit ``_stop_runner``.
        await real_asyncio.sleep(60)

    def request_stop(self) -> None:
        return None


@pytest.mark.asyncio
async def test_stop_runner_cancels_task_when_wait_for_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.scheduler.app.main as scheduler_main

    fake_runner = _HungRunner()
    monkeypatch.setattr(scheduler_main, "runner", fake_runner)
    monkeypatch.setattr(scheduler_main, "_runner_task", None)

    # Replace the module's asyncio reference so the hardcoded 5.0s timeout
    # collapses to ~10ms during this test only.
    class _FastWaitAsyncio:
        TimeoutError = real_asyncio.TimeoutError
        CancelledError = real_asyncio.CancelledError

        @staticmethod
        def create_task(
            coro: "real_asyncio.coroutines.Coroutine[object, object, object]",
            *,
            name: str | None = None,
        ) -> real_asyncio.Task[object]:
            return real_asyncio.create_task(coro, name=name)

        @staticmethod
        async def wait_for(awaitable: object, timeout: float) -> object:
            return await real_asyncio.wait_for(awaitable, 0.01)

    monkeypatch.setattr(scheduler_main, "asyncio", _FastWaitAsyncio)

    try:
        await scheduler_main._start_runner()
        task = scheduler_main._runner_task
        assert task is not None

        await scheduler_main._stop_runner()

        # Wait for the cancellation to settle so the task is marked done.
        for _ in range(10):
            if task.done():
                break
            await real_asyncio.sleep(0.01)
        assert task.done()
        assert scheduler_main._runner_task is None
    finally:
        monkeypatch.setattr(scheduler_main, "_runner_task", None)


@pytest.mark.asyncio
async def test_stop_runner_when_no_task_started_is_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling shutdown without a prior startup must not raise."""
    import services.scheduler.app.main as scheduler_main

    fake_runner = _QuickStoppingRunner()
    monkeypatch.setattr(scheduler_main, "runner", fake_runner)
    monkeypatch.setattr(scheduler_main, "_runner_task", None)

    await scheduler_main._stop_runner()
    assert scheduler_main._runner_task is None
