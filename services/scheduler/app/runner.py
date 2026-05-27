"""Tickless polling runner for the scheduler service (Story 12.08).

The runner wakes every ``tick_seconds`` (60 by default), calls each
registered job in order, and logs a ``scheduler_tick_completed`` line
with per-job timings. There is no concurrency — jobs run sequentially
on a single asyncio loop, matching the single-process deployment
constraint described in the story.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)


class _Job(Protocol):
    name: str

    async def run(self) -> Any: ...


class SchedulerRunner:
    def __init__(
        self,
        *,
        jobs: list[_Job],
        tick_seconds: float = 60.0,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        self._jobs = list(jobs)
        self._tick_seconds = tick_seconds
        self._sleep = sleep or asyncio.sleep
        self._stop = asyncio.Event()

    @property
    def jobs(self) -> tuple[_Job, ...]:
        return tuple(self._jobs)

    def request_stop(self) -> None:
        self._stop.set()

    async def tick_once(self) -> dict[str, float]:
        """Run every job once; return ``{job_name: elapsed_ms}`` map."""
        timings: dict[str, float] = {}
        for job in self._jobs:
            started = time.perf_counter()
            try:
                await job.run()
            except Exception as exc:  # broad: one job's failure cannot kill the loop
                logger.warning(
                    "scheduler_job_failed",
                    extra={"job_name": job.name, "error": repr(exc)},
                )
            timings[job.name] = (time.perf_counter() - started) * 1000.0
        logger.info("scheduler_tick_completed", extra={"timings_ms": timings})
        return timings

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            await self.tick_once()
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._tick_seconds
                )
            except asyncio.TimeoutError:
                continue


__all__ = ["SchedulerRunner"]
