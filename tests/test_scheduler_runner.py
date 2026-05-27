"""Tests for the scheduler ``SchedulerRunner`` polling loop."""

from __future__ import annotations

import asyncio

import pytest

from services.scheduler.app.runner import SchedulerRunner


class _CountingJob:
    name = "counter"

    def __init__(self) -> None:
        self.calls = 0

    async def run(self) -> None:
        self.calls += 1


class _FailingJob:
    name = "failing"

    def __init__(self) -> None:
        self.calls = 0

    async def run(self) -> None:
        self.calls += 1
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_tick_once_runs_every_job_in_order() -> None:
    a = _CountingJob()
    b = _CountingJob()
    runner = SchedulerRunner(jobs=[a, b])
    timings = await runner.tick_once()
    assert a.calls == 1
    assert b.calls == 1
    assert set(timings.keys()) == {"counter"}  # both share the name


@pytest.mark.asyncio
async def test_one_failing_job_does_not_kill_the_tick() -> None:
    bad = _FailingJob()
    good = _CountingJob()
    runner = SchedulerRunner(jobs=[bad, good])
    await runner.tick_once()
    assert bad.calls == 1
    assert good.calls == 1


@pytest.mark.asyncio
async def test_request_stop_breaks_run_forever() -> None:
    job = _CountingJob()
    runner = SchedulerRunner(jobs=[job], tick_seconds=0.01)
    task = asyncio.create_task(runner.run_forever())
    await asyncio.sleep(0.05)
    runner.request_stop()
    await asyncio.wait_for(task, timeout=1.0)
    assert job.calls >= 1


def test_jobs_property_exposes_tuple() -> None:
    job = _CountingJob()
    runner = SchedulerRunner(jobs=[job])
    assert runner.jobs == (job,)
