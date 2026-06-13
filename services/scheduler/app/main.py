"""Scheduler service entrypoint.

Promoted from heartbeat-only (pre-12.08) to a real job runner. A single
asyncio background task drives the ``SchedulerRunner`` once FastAPI's
startup hook fires. The runner currently registers one job —
``ProactiveFollowupJob`` — but new jobs slot in by extending
``_build_jobs``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from platform_common.app_factory import create_service_app
from platform_common.settings import get_settings
from services.api.app.usage.migrations import bootstrap_usage_db
from services.api.app.usage.repositories import (
    UsageDailySummaryRepository,
    UsageHitlEventRepository,
    UsageLlmCallRepository,
    UsageMessageRepository,
)
from services.scheduler.app.api_client import ApiClient
from services.scheduler.app.jobs.proactive_followup import ProactiveFollowupJob
from services.scheduler.app.jobs.usage_retention_job import UsageRetentionJob
from services.scheduler.app.jobs.usage_rollup_job import UsageRollupJob
from services.scheduler.app.runner import SchedulerRunner
from services.scheduler.app.usage_rollup import RollupRepos

app = create_service_app("scheduler")
logger = logging.getLogger(__name__)
settings = get_settings()


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _default_project_tz_lookup(_project_id: int) -> str:
    """Project tz lookup with settings fallback.

    The api owns the `hitl_runtime_config` table; the scheduler does
    not open SQLite directly. Until a dedicated endpoint exists, fall
    back to the platform-wide default tz — Epic 11 stores the override
    there too. This keeps the job correct for single-tenant deployments
    (the common case in v1).
    """
    return settings.default_timezone


def _build_jobs() -> list[Any]:
    api_client = ApiClient(
        base_url=settings.api_internal_base_url,
        service_token=settings.internal_service_token or "",
    )
    usage_db = settings.usage_db_path
    bootstrap_usage_db(usage_db)
    rollup_repos = RollupRepos(
        llm=UsageLlmCallRepository(db_path=usage_db),
        messages=UsageMessageRepository(db_path=usage_db),
        hitl=UsageHitlEventRepository(db_path=usage_db),
        summary=UsageDailySummaryRepository(db_path=usage_db),
        db_path=usage_db,
    )
    state_path = str(Path(usage_db).parent / "scheduler_runs.json")
    return [
        ProactiveFollowupJob(
            api_client=api_client,
            clock=_default_clock,
            project_tz_lookup=_default_project_tz_lookup,
        ),
        UsageRollupJob(
            repos=rollup_repos,
            clock=_default_clock,
            state_path=state_path,
            rollup_hour_utc=settings.usage_daily_rollup_hour_utc,
        ),
        UsageRetentionJob(
            repos=rollup_repos,
            clock=_default_clock,
            state_path=state_path,
            rollup_hour_utc=settings.usage_daily_rollup_hour_utc,
            retention_days=settings.usage_raw_retention_days,
            batch_size=settings.usage_rollup_batch_size,
        ),
    ]


runner = SchedulerRunner(jobs=_build_jobs(), tick_seconds=60.0)
_runner_task: asyncio.Task[None] | None = None


@app.on_event("startup")
async def _start_runner() -> None:
    global _runner_task
    if _runner_task is not None and not _runner_task.done():
        return
    _runner_task = asyncio.create_task(
        runner.run_forever(), name="scheduler_runner"
    )
    logger.info("scheduler_runner_started")


@app.on_event("shutdown")
async def _stop_runner() -> None:
    global _runner_task
    runner.request_stop()
    if _runner_task is not None:
        try:
            await asyncio.wait_for(_runner_task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _runner_task.cancel()
    _runner_task = None
    logger.info("scheduler_runner_stopped")
